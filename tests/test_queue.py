from datetime import date
from pathlib import Path

import pytest

from video_builder_publisher.queue import PublishQueue


def test_stage_locate_and_move(tmp_path: Path) -> None:
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video-bytes")
    queue = PublishQueue(tmp_path / "queue")

    staged, digest = queue.stage(
        source,
        date(2026, 8, 20),
        "demo|2026-08-20",
        {"title": "demo"},
    )
    assert staged.is_file()
    assert queue.locate("demo|2026-08-20", digest) == staged

    moved = queue.move_state(staged, "demo|2026-08-20", "published")
    assert moved.is_file()
    assert "published" in moved.parts
    assert queue.locate("demo|2026-08-20", digest) == moved


def test_manifest_rejects_secret_like_keys(tmp_path: Path) -> None:
    source = tmp_path / "render.mp4"
    source.write_bytes(b"video")
    queue = PublishQueue(tmp_path / "queue")
    with pytest.raises(ValueError):
        queue.stage(
            source,
            date(2026, 8, 20),
            "demo",
            {"access_token": "should-never-be-here"},
        )
