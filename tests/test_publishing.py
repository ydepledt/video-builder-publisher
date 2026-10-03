import pytest

from video_builder_publisher.publishing import (
    InstagramConfig,
    InstagramPublisher,
    PublishError,
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


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.text = "fake response"

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses) -> None:
        self.responses = iter(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(url)
        return next(self.responses)


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(ValueError("invalid JSON")),
        FakeResponse([]),
        FakeResponse({}),
        FakeResponse({}, status_code=503),
    ],
    ids=["invalid-json", "non-object-json", "missing-id", "server-error"],
)
def test_instagram_ambiguous_publish_response_preserves_container(
    tmp_path, monkeypatch, response
) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"offline-test-video")
    session = FakeSession(
        [FakeResponse({"id": "12345"}), FakeResponse({"success": True}), response]
    )
    publisher = InstagramPublisher(InstagramConfig("test-token", "123", "v25.0"), session)
    monkeypatch.setattr(publisher, "_wait_ready", lambda container_id: None)

    with pytest.raises(PublishError) as caught:
        publisher.publish(video, "Test", "Test description")
    assert session.calls[-1].endswith("/media_publish")
    assert caught.value.outcome_uncertain is True
    assert caught.value.external_id == "12345"


def test_instagram_explicit_publish_rejection_remains_a_failure(tmp_path, monkeypatch) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"offline-test-video")
    session = FakeSession(
        [
            FakeResponse({"id": "12345"}),
            FakeResponse({"success": True}),
            FakeResponse({}, status_code=400),
        ]
    )
    publisher = InstagramPublisher(InstagramConfig("test-token", "123", "v25.0"), session)
    monkeypatch.setattr(publisher, "_wait_ready", lambda container_id: None)

    with pytest.raises(PublishError) as caught:
        publisher.publish(video, "Test", "Test description")
    assert caught.value.outcome_uncertain is False


@pytest.mark.parametrize(
    "response",
    [
        FakeResponse(ValueError("invalid JSON")),
        FakeResponse([]),
        FakeResponse({"data": []}),
        FakeResponse({"error": {"code": "access_token_invalid"}}),
        FakeResponse({"error": None}),
        FakeResponse({"error": {"code": []}}),
        FakeResponse({}, status_code=401),
        FakeResponse({}, status_code=503),
    ],
    ids=[
        "invalid-json", "non-object-json", "invalid-data", "api-error", "invalid-error",
        "invalid-error-code", "unauthorized", "server-error",
    ],
)
def test_tiktok_status_lookup_failure_preserves_upload_id(response) -> None:
    publisher = TikTokPublisher(TikTokConfig("test-token"), FakeSession([response]))
    with pytest.raises(PublishError) as caught:
        publisher._poll_status("upload-123", accepted={"SEND_TO_USER_INBOX"}, timeout_seconds=1)
    assert caught.value.outcome_uncertain is True
    assert caught.value.external_id == "upload-123"


def test_tiktok_explicit_processing_failure_remains_retryable() -> None:
    response = FakeResponse({"data": {"status": "FAILED", "fail_reason": "invalid video"}})
    publisher = TikTokPublisher(TikTokConfig("test-token"), FakeSession([response]))
    with pytest.raises(PublishError) as caught:
        publisher._poll_status("upload-123", accepted={"SEND_TO_USER_INBOX"}, timeout_seconds=1)
    assert caught.value.outcome_uncertain is False
