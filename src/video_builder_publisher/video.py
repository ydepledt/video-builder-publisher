"""Reusable video output primitives shared by content generators."""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - fixed argv execution only, never shell=True
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True, slots=True)
class VideoSpec:
    width: int
    height: int
    fps: int
    codec: str = "libx264"
    pixel_format: str = "yuv420p"
    crf: int = 18
    preset: str = "medium"

    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0:
            raise ValueError("Video dimensions must be positive.")
        if self.fps <= 0:
            raise ValueError("fps must be positive.")
        if not 0 <= self.crf <= 51:
            raise ValueError("crf must be between 0 and 51.")


VIDEO_PROFILES: dict[str, VideoSpec] = {
    "shorts": VideoSpec(1080, 1920, 60, crf=18, preset="fast"),
    "social": VideoSpec(1080, 1920, 30, crf=18, preset="medium"),
    "landscape": VideoSpec(1920, 1080, 60, crf=18, preset="fast"),
}


def resolve_video_spec(
    profile: str,
    *,
    width: int | None = None,
    height: int | None = None,
    fps: int | None = None,
    crf: int | None = None,
    preset: str | None = None,
) -> VideoSpec:
    """Resolve a named profile with optional explicit overrides."""

    try:
        base = VIDEO_PROFILES[profile]
    except KeyError as exc:
        valid = ", ".join(sorted(VIDEO_PROFILES))
        raise ValueError(f"Unknown video profile {profile!r}. Expected one of: {valid}.") from exc

    return replace(
        base,
        width=base.width if width is None else int(width),
        height=base.height if height is None else int(height),
        fps=base.fps if fps is None else int(fps),
        crf=base.crf if crf is None else int(crf),
        preset=base.preset if preset is None else str(preset),
    )


def _ensure_tool(name: str) -> str:
    executable = shutil.which(name)
    if not executable:
        raise RuntimeError(f"{name} is required but was not found in PATH.")
    return executable


@contextmanager
def atomic_video_target(target: str | Path) -> Iterator[Path]:
    """Yield a same-directory temporary MP4 and atomically replace target on success."""

    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.stem}.",
        suffix=".tmp.mp4",
        dir=target_path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        yield temp_path
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("Video renderer produced an empty output file.")
        with temp_path.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target_path)
    finally:
        temp_path.unlink(missing_ok=True)


def probe_video(path: str | Path) -> dict:
    """Return ffprobe JSON for one media file."""

    ffprobe = _ensure_tool("ffprobe")
    result = subprocess.run(  # nosec B603 - argv list, no shell interpolation
        [
            ffprobe,
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "ffprobe failed").strip()
        raise RuntimeError(f"Unable to probe video: {message}")
    return json.loads(result.stdout)


def _fps_value(raw: str | None) -> float:
    if not raw or raw == "0/0":
        return 0.0
    try:
        return float(Fraction(raw))
    except (ValueError, ZeroDivisionError):
        return 0.0


def validate_video(
    path: str | Path,
    spec: VideoSpec,
    *,
    min_duration: float = 0.0,
    require_audio: bool | None = None,
    fps_tolerance: float = 0.5,
) -> dict:
    """Validate encoded dimensions, fps, duration and optional audio presence."""

    data = probe_video(path)
    streams = data.get("streams") or []
    video_stream = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video_stream is None:
        raise RuntimeError("Encoded file has no video stream.")

    width = int(video_stream.get("width") or 0)
    height = int(video_stream.get("height") or 0)
    if (width, height) != (spec.width, spec.height):
        raise RuntimeError(
            f"Unexpected video size {width}x{height}; expected {spec.width}x{spec.height}."
        )

    actual_fps = _fps_value(video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate"))
    if actual_fps and abs(actual_fps - spec.fps) > fps_tolerance:
        raise RuntimeError(f"Unexpected fps {actual_fps:.3f}; expected {spec.fps}.")

    raw_duration = (data.get("format") or {}).get("duration") or video_stream.get("duration") or 0
    duration = float(raw_duration)
    if duration + 1e-6 < min_duration:
        raise RuntimeError(
            f"Encoded duration {duration:.3f}s is below required {min_duration:.3f}s."
        )

    has_audio = any(stream.get("codec_type") == "audio" for stream in streams)
    if require_audio is True and not has_audio:
        raise RuntimeError("Encoded file has no audio stream.")
    if require_audio is False and has_audio:
        raise RuntimeError("Encoded file unexpectedly contains an audio stream.")

    return data


def mux_audio_track(
    video_path: str | Path,
    audio_path: str | Path,
    target: str | Path | None = None,
    *,
    audio_codec: str = "aac",
    audio_bitrate: str = "192k",
) -> Path:
    """Mux audio into an existing video without re-encoding its video stream."""

    ffmpeg = _ensure_tool("ffmpeg")
    source_video = Path(video_path)
    source_audio = Path(audio_path)
    target_path = source_video if target is None else Path(target)

    if not source_video.is_file():
        raise FileNotFoundError(source_video)
    if not source_audio.is_file():
        raise FileNotFoundError(source_audio)

    with atomic_video_target(target_path) as temp_path:
        result = subprocess.run(  # nosec B603 - argv list, no shell interpolation
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source_video),
                "-i",
                str(source_audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                audio_codec,
                "-b:a",
                audio_bitrate,
                "-shortest",
                "-movflags",
                "+faststart",
                str(temp_path),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "ffmpeg mux failed").strip()
            raise RuntimeError(f"Unable to mux audio: {message}")
    return target_path


class RawVideoEncoder:
    """Stream packed BGR24 frames to FFmpeg with atomic publication."""

    def __init__(self, target: str | Path, spec: VideoSpec) -> None:
        self.target = Path(target)
        self.spec = spec
        self._context = atomic_video_target(self.target)
        self._temp_path = self._context.__enter__()
        ffmpeg = _ensure_tool("ffmpeg")
        self._proc = subprocess.Popen(  # nosec B603 - argv list, no shell interpolation
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "rawvideo",
                "-vcodec",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "-s",
                f"{spec.width}x{spec.height}",
                "-r",
                str(spec.fps),
                "-i",
                "-",
                "-an",
                "-c:v",
                spec.codec,
                "-preset",
                spec.preset,
                "-crf",
                str(spec.crf),
                "-pix_fmt",
                spec.pixel_format,
                "-movflags",
                "+faststart",
                str(self._temp_path),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._closed = False

    def write_frame(self, frame) -> None:
        if self._closed or self._proc.stdin is None:
            raise RuntimeError("Encoder is already closed.")
        expected_shape = (self.spec.height, self.spec.width, 3)
        if getattr(frame, "shape", None) != expected_shape:
            raise ValueError(
                f"Unexpected frame shape {getattr(frame, 'shape', None)}; expected {expected_shape}."
            )
        self._proc.stdin.write(frame.tobytes())

    def close(self, *, min_duration: float = 0.0) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            stderr = b""
            if self._proc.stderr is not None:
                stderr = self._proc.stderr.read()
            self._proc.wait()
            if self._proc.returncode != 0:
                message = stderr.decode("utf-8", errors="replace").strip() or "unknown FFmpeg error"
                raise RuntimeError(f"ffmpeg exited with code {self._proc.returncode}: {message}")
            validate_video(
                self._temp_path,
                self.spec,
                min_duration=min_duration,
                require_audio=False,
            )
        except Exception as exc:
            self._context.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            self._context.__exit__(None, None, None)

    def abort(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._proc.poll() is None:
            self._proc.kill()
        if self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        self._proc.wait()
        self._context.__exit__(RuntimeError, RuntimeError("Encoding aborted."), None)

    def __enter__(self) -> "RawVideoEncoder":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()
