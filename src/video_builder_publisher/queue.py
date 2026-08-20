"""Immutable artifact queue for generated videos."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import date
from pathlib import Path

from .security import (
    atomic_write_json,
    ensure_private_dir,
    reject_secret_like_keys,
    sha256_file,
)


class PublishQueue:
    """Stage immutable artifacts and move their run folders through queue states."""

    STATES = ("ready", "submitted", "published", "failed", "needs_review")

    def __init__(self, root: str | Path) -> None:
        self.root = ensure_private_dir(root)

    @staticmethod
    def run_folder_name(run_key: str) -> str:
        readable = re.sub(r"[^A-Za-z0-9._-]+", "_", run_key).strip("._-")
        digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()[:12]
        return f"{readable[:72] or 'run'}--{digest}"

    def _folder(self, state: str, run_key: str) -> Path:
        if state not in self.STATES:
            raise ValueError(f"Unsupported queue state: {state}")
        return self.root / state / self.run_folder_name(run_key)

    def stage(
        self,
        video_path: str | Path,
        run_date: date,
        run_key: str,
        manifest: dict,
    ) -> tuple[Path, str]:
        """Copy a rendered artifact atomically into its unique logical run folder."""

        source = Path(video_path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError(f"Cannot stage missing/empty artifact: {source}")

        reject_secret_like_keys(manifest, path="manifest")
        digest = sha256_file(source)
        folder = ensure_private_dir(self._folder("ready", run_key))
        target = folder / source.name

        if target.exists():
            if not target.is_file() or sha256_file(target) != digest:
                raise RuntimeError(f"Queue artifact collision/integrity mismatch for run {run_key}")
        else:
            fd, temp_name = tempfile.mkstemp(
                prefix=f".{source.name}.", suffix=".tmp", dir=folder
            )
            os.close(fd)
            temp = Path(temp_name)
            try:
                shutil.copyfile(source, temp)
                if os.name != "nt":
                    os.chmod(temp, 0o600)
                if sha256_file(temp) != digest:
                    raise RuntimeError("Staged artifact failed SHA-256 verification")
                os.replace(temp, target)
            except Exception:
                temp.unlink(missing_ok=True)
                raise

        safe_manifest = dict(manifest)
        safe_manifest["run_key"] = run_key
        safe_manifest["date"] = run_date.isoformat()
        safe_manifest["artifact"] = target.name
        safe_manifest["sha256"] = digest
        atomic_write_json(folder / "manifest.json", safe_manifest)
        return target, digest

    def locate(self, run_key: str, expected_sha256: str | None = None) -> Path | None:
        """Find a staged artifact across queue states and verify its digest."""

        for state in self.STATES:
            folder = self._folder(state, run_key)
            manifest_path = folder / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            artifact_name = Path(str(manifest.get("artifact", ""))).name
            if not artifact_name:
                continue
            candidate = folder / artifact_name
            if not candidate.is_file() or candidate.stat().st_size <= 0:
                continue

            manifest_sha = str(manifest.get("sha256", ""))
            actual_sha = sha256_file(candidate)
            if manifest_sha and actual_sha != manifest_sha:
                raise RuntimeError(f"Queue integrity mismatch for run {run_key}: {candidate}")
            if expected_sha256 and actual_sha != expected_sha256:
                raise RuntimeError(
                    f"Stored artifact SHA-256 mismatch for run {run_key}: {candidate}"
                )
            return candidate
        return None

    def move_state(self, artifact: str | Path, run_key: str, state: str) -> Path:
        """Atomically move one logical run folder to another queue state."""

        if state not in self.STATES or state == "ready":
            raise ValueError(f"Unsupported queue target state: {state}")

        source = Path(artifact)
        if not source.is_file():
            located = self.locate(run_key)
            if located is None:
                return source
            source = located

        source_dir = source.parent
        expected_dir_name = self.run_folder_name(run_key)
        if source_dir.name != expected_dir_name:
            raise RuntimeError(f"Artifact does not belong to expected queue run folder: {source}")

        target_dir = self._folder(state, run_key)
        ensure_private_dir(target_dir.parent)
        if source_dir == target_dir:
            return source
        if target_dir.exists():
            raise RuntimeError(f"Queue target already exists for run {run_key}: {target_dir}")

        os.replace(source_dir, target_dir)
        moved = target_dir / source.name
        if not moved.is_file():
            raise RuntimeError(f"Queue move lost artifact for run {run_key}")
        return moved
