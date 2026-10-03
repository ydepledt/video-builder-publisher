from datetime import date
from pathlib import Path

import pytest

from video_builder_publisher.publication_service import (
    PublicationArtifact,
    PublicationService,
)
from video_builder_publisher.publishing import PublishError, PublishResult
from video_builder_publisher.queue import PublishQueue
from video_builder_publisher.store import PublicationStore


class FakePublisher:
    def __init__(self, platform: str, status: str = "published") -> None:
        self.platform = platform
        self.status = status
        self.calls = []

    def publish(self, video: Path, title: str, description: str) -> PublishResult:
        self.calls.append((video, title, description))
        return PublishResult(self.status, f"{self.platform}-123")


def test_stage_and_publish_arbitrary_run_key(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"not-a-real-video-but-non-empty")
    publishers = {"youtube": FakePublisher("youtube")}
    service = PublicationService(
        store=PublicationStore(tmp_path / "state" / "publication.sqlite"),
        queue=PublishQueue(tmp_path / "state" / "queue"),
        publisher_factory=publishers.__getitem__,
    )
    artifact = PublicationArtifact(
        run_key="quiz|cmp-abc|v0001",
        scope_key="quiz",
        run_date=date(2026, 8, 25),
        content_format="quiz.v1",
        selection_signature="episode-abc",
        video_path=video,
        manifest={"title": "Quiz", "description": "Test", "pack": "capitals"},
    )

    staged = service.stage(artifact)
    assert staged["queue_state"] == "needs_review"
    assert service.store.get_run(artifact.run_key).status == "rendered"

    published = service.publish(artifact.run_key, ["youtube"], approved=True)
    assert published["run_status"] == "completed"
    assert published["queue_state"] == "published"
    assert publishers["youtube"].calls[0][1:] == ("Quiz", "Test")


def test_stage_is_idempotent_for_same_bytes(tmp_path: Path) -> None:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"same")
    service = PublicationService(
        store=PublicationStore(tmp_path / "publication.sqlite"),
        queue=PublishQueue(tmp_path / "queue"),
        publisher_factory=lambda platform: FakePublisher(platform),
    )
    artifact = PublicationArtifact(
        run_key="satisfying|concept-1|seed-2",
        scope_key="satisfying",
        run_date=date(2026, 8, 25),
        content_format="satisfying.v1",
        selection_signature="sig",
        video_path=video,
        manifest={"title": "Satisfying"},
    )

    first = service.stage(artifact)
    second = service.stage(artifact)
    assert first["sha256"] == second["sha256"]
    assert second["queue_state"] == "needs_review"


def stage_test_artifact(tmp_path: Path, publisher_factory) -> tuple[PublicationService, str]:
    video = tmp_path / "video.mp4"
    video.write_bytes(b"offline-test-video")
    service = PublicationService(
        store=PublicationStore(tmp_path / "publication.sqlite"),
        queue=PublishQueue(tmp_path / "queue"),
        publisher_factory=publisher_factory,
    )
    artifact = PublicationArtifact(
        run_key="test|ambiguous-upload",
        scope_key="test",
        run_date=date(2026, 8, 25),
        content_format="test.v1",
        selection_signature="sig",
        video_path=video,
        manifest={"title": "Test"},
    )
    service.stage(artifact)
    return service, artifact.run_key


class FailingPublisher(FakePublisher):
    def __init__(self, error: Exception) -> None:
        super().__init__("youtube")
        self.error = error

    def publish(self, video: Path, title: str, description: str) -> PublishResult:
        super().publish(video, title, description)
        # A remote upload may already have completed before response handling fails.
        raise self.error


def test_unexpected_publish_exception_is_not_retried(tmp_path: Path) -> None:
    publisher = FailingPublisher(RuntimeError("response processing failed"))
    other_publisher = FakePublisher("instagram")
    publishers = {"youtube": publisher, "instagram": other_publisher}
    service, run_key = stage_test_artifact(tmp_path, publishers.__getitem__)

    for _ in range(2):
        result = service.publish(run_key, publishers, approved=True)
        assert result["platforms"] == {"youtube": "unknown", "instagram": "published"}
        assert result["run_status"] == "needs_review"
        assert result["queue_state"] == "needs_review"
    assert len(publisher.calls) == 1
    assert len(other_publisher.calls) == 1


def test_publisher_factory_failure_can_be_retried(tmp_path: Path) -> None:
    publisher = FakePublisher("youtube")
    attempts = []

    def factory(platform: str):
        attempts.append(platform)
        if len(attempts) == 1:
            raise ValueError("publisher configuration is missing")
        return publisher

    service, run_key = stage_test_artifact(tmp_path, factory)
    first = service.publish(run_key, ["youtube"], approved=True)
    assert first["platforms"] == {"youtube": "failed"}
    second = service.publish(run_key, ["youtube"], approved=True)
    assert second["platforms"] == {"youtube": "published"}
    assert len(publisher.calls) == 1


@pytest.mark.parametrize("uncertain, status, calls", [(False, "failed", 2), (True, "unknown", 1)])
def test_explicit_publish_error_controls_retry(
    tmp_path: Path, uncertain: bool, status: str, calls: int
) -> None:
    publisher = FailingPublisher(PublishError("upload failed", outcome_uncertain=uncertain))
    service, run_key = stage_test_artifact(tmp_path, lambda platform: publisher)
    for _ in range(2):
        result = service.publish(run_key, ["youtube"], approved=True)
        assert result["platforms"] == {"youtube": status}
    assert len(publisher.calls) == calls
