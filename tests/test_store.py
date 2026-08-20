from datetime import date
from pathlib import Path

from video_builder_publisher.store import PublicationStore


def test_publication_store_happy_path(tmp_path: Path) -> None:
    store = PublicationStore(tmp_path / "state" / "publications.sqlite3")
    run_key = store.begin_run("demo", date(2026, 8, 20))
    assert run_key == "demo|2026-08-20"

    store.mark_rendered(run_key, "top", "sig", "/tmp/video.mp4", "abc")
    run = store.get_run(run_key)
    assert run is not None
    assert run.scope_key == "demo"
    assert run.status == "rendered"

    assert store.begin_platform(run_key, "youtube") is True
    store.finish_platform(run_key, "youtube", "published", "video-id")
    assert store.finalize_run(run_key) == "completed"
    assert store.begin_platform(run_key, "youtube") is False


def test_interrupted_publication_fails_closed(tmp_path: Path) -> None:
    store = PublicationStore(tmp_path / "publications.sqlite3")
    run_key = store.begin_run("demo", date(2026, 8, 20))
    assert store.begin_platform(run_key, "instagram") is True
    assert store.recover_interrupted_publications(run_key) == 1
    assert store.platform_statuses(run_key)["instagram"] == "unknown"
    assert store.finalize_run(run_key) == "needs_review"
