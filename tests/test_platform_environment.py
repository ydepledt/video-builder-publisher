from pathlib import Path

import pytest

from video_builder_publisher.platform_environment import PlatformEnvironment


def test_missing_declared_token_file_is_not_configured(monkeypatch, tmp_path: Path) -> None:
    env = PlatformEnvironment("quiz")
    monkeypatch.setenv("QUIZ_YOUTUBE_TOKEN_FILE", str(tmp_path / "missing.json"))
    monkeypatch.setenv("QUIZ_TIKTOK_ACCESS_TOKEN_FILE", str(tmp_path / "missing-token"))

    assert env.publisher_configured("youtube") is False
    assert env.publisher_configured("tiktok") is False
    assert env.analytics_configured("youtube") is False
    assert env.analytics_configured("tiktok") is False


def test_analytics_falls_back_to_publication_credentials(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "youtube.json"
    token_file.write_text("{}", encoding="utf-8")
    secret_file = tmp_path / "tiktok.token"
    secret_file.write_text("abc", encoding="utf-8")
    secret_file.chmod(0o600)

    monkeypatch.setenv("SATISFYING_YOUTUBE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("SATISFYING_TIKTOK_ACCESS_TOKEN_FILE", str(secret_file))
    env = PlatformEnvironment("satisfying")

    assert env.analytics_configured("youtube") is True
    assert env.analytics_configured("tiktok") is True
    assert env.build_analytics_client("youtube").config.token_file == token_file
    assert env.build_analytics_client("tiktok").config.access_token == "abc"


def test_public_youtube_requires_explicit_opt_in(monkeypatch, tmp_path: Path) -> None:
    token_file = tmp_path / "youtube.json"
    token_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("QUIZ_YOUTUBE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("QUIZ_YOUTUBE_PRIVACY", "public")
    env = PlatformEnvironment("quiz")

    with pytest.raises(RuntimeError, match="Public YouTube upload is disabled"):
        env.build_publisher("youtube")

    monkeypatch.setenv("QUIZ_ALLOW_PUBLIC_YOUTUBE", "1")
    publisher = env.build_publisher("youtube")
    assert publisher.config.privacy == "public"


def test_instagram_requires_user_id_for_publishing_but_not_analytics(monkeypatch) -> None:
    monkeypatch.setenv("QUIZ_INSTAGRAM_ACCESS_TOKEN", "secret")
    env = PlatformEnvironment("quiz")

    assert env.publisher_configured("instagram") is False
    assert env.analytics_configured("instagram") is True
