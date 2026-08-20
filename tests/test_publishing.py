import pytest

from video_builder_publisher.publishing import (
    InstagramConfig,
    TikTokConfig,
    TikTokPublisher,
    YouTubeConfig,
)


def test_tiktok_chunk_plan_rounds_up() -> None:
    chunk_size, count = TikTokPublisher._chunk_plan(50 * 1024 * 1024)
    assert chunk_size == 32 * 1024 * 1024
    assert count == 2


def test_tiktok_direct_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        TikTokConfig("token", mode="direct")


def test_instagram_config_validation() -> None:
    with pytest.raises(ValueError):
        InstagramConfig("token", "not-a-number", "v25.0")
    with pytest.raises(ValueError):
        InstagramConfig("token", "123", "25")


def test_youtube_privacy_validation(tmp_path) -> None:
    with pytest.raises(ValueError):
        YouTubeConfig(tmp_path / "token.json", privacy="friends")
