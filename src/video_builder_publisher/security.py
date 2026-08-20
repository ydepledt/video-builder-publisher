"""Security helpers shared by renderers and publishers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

SECRET_KEY_FRAGMENTS = (
    "token",
    "secret",
    "password",
    "api_key",
    "client_secret",
    "access_key",
)
URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)\?[^\s]+", re.IGNORECASE)
AUTH_RE = re.compile(r"(?i)\b(Bearer|OAuth)\s+[A-Za-z0-9._~+/=-]+")
QUERY_SECRET_RE = re.compile(
    r"(?i)(access_token|refresh_token|upload_token|token|api_key|client_secret)=([^&\s]+)"
)
MAX_SECRET_FILE_BYTES = 64 * 1024
DOCKER_SECRETS_ROOT = Path("/run/secrets")


def reject_secret_like_keys(data: Any, path: str = "settings") -> None:
    """Reject credentials accidentally embedded in normal config mappings."""

    if isinstance(data, dict):
        for key, value in data.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if any(fragment in normalized for fragment in SECRET_KEY_FRAGMENTS):
                raise ValueError(
                    f"Secret-like setting '{path}.{key}' is not allowed in normal config; "
                    "use environment variables or secret files."
                )
            reject_secret_like_keys(value, f"{path}.{key}")
    elif isinstance(data, list):
        for index, value in enumerate(data):
            reject_secret_like_keys(value, f"{path}[{index}]")


def _is_docker_secret(path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(DOCKER_SECRETS_ROOT)
    except OSError:
        return False


def require_env(name: str) -> str:
    """Read NAME or NAME_FILE using the common container-secret convention."""

    direct = os.environ.get(name, "").strip()
    file_value = os.environ.get(f"{name}_FILE", "").strip()
    if direct and file_value:
        raise RuntimeError(f"Set only one of {name} or {name}_FILE")
    if direct:
        return direct
    if file_value:
        path = Path(file_value)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Secret file is missing or not a regular file: {path}")
        if path.stat().st_size > MAX_SECRET_FILE_BYTES:
            raise RuntimeError(f"Secret file is unexpectedly large: {path}")
        if not _is_docker_secret(path):
            assert_private_file(path, f"{name}_FILE")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise RuntimeError(f"Cannot read secret file for {name}") from exc
        if not value:
            raise RuntimeError(f"Secret file for {name} is empty")
        return value
    raise RuntimeError(f"Missing required environment variable: {name} (or {name}_FILE)")


def ensure_private_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(target, 0o700)
    return target


def assert_private_file(path: str | Path, label: str = "Sensitive file") -> Path:
    target = Path(path)
    if not target.is_file():
        raise RuntimeError(f"{label} does not exist or is not a regular file: {target}")
    if os.name != "nt":
        mode = target.stat().st_mode & 0o777
        if mode & 0o077:
            raise RuntimeError(
                f"{label} must not be group/world-accessible: {target} "
                f"(mode {mode:03o}; expected 600 or stricter)"
            )
    return target


def redact_sensitive_text(value: Any, secret_values: Iterable[str] = ()) -> str:
    """Return a log-safe representation with explicit secrets and auth/query tokens redacted."""

    text = str(value)
    for secret in secret_values:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    text = AUTH_RE.sub(lambda match: f"{match.group(1)} <redacted>", text)
    text = QUERY_SECRET_RE.sub(lambda match: f"{match.group(1)}=<redacted>", text)
    text = URL_QUERY_RE.sub(r"\1?<redacted-query>", text)
    return text[:2000]


def validate_https_url(url: str, allowed_hosts: tuple[str, ...]) -> str:
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https":
        raise ValueError("Only HTTPS URLs are allowed.")
    if parsed.username or parsed.password:
        raise ValueError("Credential-bearing URLs are not allowed.")
    if parsed.port not in {None, 443}:
        raise ValueError("Non-standard HTTPS ports are not allowed.")
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("URL must contain a hostname.")
    normalized = tuple(item.lower().lstrip(".") for item in allowed_hosts)
    if not any(host == item or host.endswith("." + item) for item in normalized):
        raise ValueError(f"URL host is not allowed: {host}")
    return url


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temp = Path(temp_name)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp.unlink(missing_ok=True)
        raise


def atomic_write_json(path: str | Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _linux_process_marker(pid: int) -> str | None:
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        start_ticks = stat_fields[21]
    except (OSError, IndexError):
        return None
    return f"{boot_id}:{start_ticks}"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class FileLock:
    """Atomic lock file with ownership-safe stale-lock recovery."""

    path: Path
    stale_after_seconds: int = 6 * 60 * 60
    _acquired: bool = False
    _token: str = field(default_factory=lambda: uuid.uuid4().hex)

    def acquire(self) -> None:
        ensure_private_dir(self.path.parent)
        if self.stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be greater than 0")
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not self._break_stale_lock():
                    raise RuntimeError(f"Another publisher run is active: {self.path}")
                continue
            try:
                pid = os.getpid()
                payload = {
                    "pid": pid,
                    "hostname": socket.gethostname(),
                    "process_marker": _linux_process_marker(pid),
                    "token": self._token,
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                }
                os.write(fd, json.dumps(payload).encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            self._acquired = True
            return

    def _break_stale_lock(self) -> bool:
        try:
            stat = self.path.stat()
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return True
        except OSError:
            return False

        age = max(0.0, time.time() - stat.st_mtime)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            payload = {}

        if payload.get("hostname") == socket.gethostname():
            try:
                pid = int(payload.get("pid", 0))
            except (TypeError, ValueError):
                pid = 0
            if _pid_is_alive(pid):
                stored_marker = payload.get("process_marker")
                current_marker = _linux_process_marker(pid)
                if not (stored_marker and current_marker and stored_marker != current_marker):
                    return False
            elif age <= self.stale_after_seconds:
                return False
        elif age <= self.stale_after_seconds:
            return False

        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        return True

    def release(self) -> None:
        if not self._acquired:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            payload = {}
        if payload.get("token") == self._token:
            self.path.unlink(missing_ok=True)
        self._acquired = False

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()
