from pathlib import Path

import pytest

from video_builder_publisher.security import (
    redact_sensitive_text,
    require_env,
    validate_https_url,
)


def test_redaction_removes_explicit_and_auth_secrets() -> None:
    text = redact_sensitive_text(
        "Bearer abc123 https://example.com/upload?access_token=xyz explicit-secret",
        ("explicit-secret",),
    )
    assert "abc123" not in text
    assert "xyz" not in text
    assert "explicit-secret" not in text


def test_https_allowlist_rejects_wrong_host() -> None:
    assert validate_https_url("https://upload.tiktokapis.com/a", ("tiktokapis.com",))
    with pytest.raises(ValueError):
        validate_https_url("https://tiktokapis.com.evil.example/a", ("tiktokapis.com",))


def test_require_env_supports_private_file(monkeypatch, tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("value\n", encoding="utf-8")
    secret.chmod(0o600)
    monkeypatch.setenv("DEMO_TOKEN_FILE", str(secret))
    assert require_env("DEMO_TOKEN") == "value"
