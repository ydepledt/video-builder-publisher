from datetime import date
from pathlib import Path

from video_builder_publisher.publication_service import (
    PublicationArtifact,
    PublicationService,
)
from video_builder_publisher.publishing import PublishResult
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
