from array import array
from pathlib import Path

import pytest

from video_builder_publisher.video import (
    VIDEO_PROFILES,
    VideoSpec,
    _frame_payload,
    atomic_video_target,
    resolve_video_spec,
)


def test_named_profiles_are_vertical_and_landscape() -> None:
    assert VIDEO_PROFILES["shorts"].width == 1080
    assert VIDEO_PROFILES["shorts"].height == 1920
    assert VIDEO_PROFILES["landscape"].width == 1920
    assert VIDEO_PROFILES["landscape"].height == 1080


def test_profile_overrides_do_not_mutate_global() -> None:
    custom = resolve_video_spec("shorts", fps=30, crf=20)
    assert custom.fps == 30
    assert custom.crf == 20
    assert VIDEO_PROFILES["shorts"].fps == 60


def test_video_spec_rejects_invalid_values() -> None:
    with pytest.raises(ValueError):
        VideoSpec(0, 1080, 30)
    with pytest.raises(ValueError):
        VideoSpec(1920, 1080, 0)
    with pytest.raises(ValueError):
        VideoSpec(1920, 1080, 30, crf=99)


def test_frame_payload_reuses_contiguous_buffer_without_copy() -> None:
    frame = array("B", range(12))
    payload = _frame_payload(frame, 12)
    assert isinstance(payload, memoryview)
    assert payload.obj is frame
    assert payload.tobytes() == bytes(range(12))


def test_frame_payload_rejects_wrong_byte_size() -> None:
    frame = array("I", [1, 2, 3])
    with pytest.raises(ValueError, match="frame byte size"):
        _frame_payload(frame, 3)


def test_atomic_video_target_replaces_only_on_success(tmp_path: Path) -> None:
    target = tmp_path / "video.mp4"
    target.write_bytes(b"old")
    with atomic_video_target(target) as temp:
        temp.write_bytes(b"new")
    assert target.read_bytes() == b"new"


def test_atomic_video_target_preserves_old_target_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "video.mp4"
    target.write_bytes(b"old")
    with pytest.raises(RuntimeError):
        with atomic_video_target(target):
            pass
    assert target.read_bytes() == b"old"
