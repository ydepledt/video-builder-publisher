"""Shared video building and publication primitives."""

from .publishing import (
    InstagramConfig,
    InstagramPublisher,
    PublishError,
    PublishResult,
    Publisher,
    TikTokConfig,
    TikTokPublisher,
    YouTubeConfig,
    YouTubePublisher,
    authorize_youtube,
    build_publisher,
)
from .queue import PublishQueue
from .store import PublicationStore, RunRecord
from .video import (
    VIDEO_PROFILES,
    RawVideoEncoder,
    VideoSpec,
    atomic_video_target,
    mux_audio_track,
    probe_video,
    resolve_video_spec,
    validate_video,
)

__all__ = [
    "VIDEO_PROFILES",
    "InstagramConfig",
    "InstagramPublisher",
    "PublishError",
    "PublishQueue",
    "PublishResult",
    "PublicationStore",
    "Publisher",
    "RawVideoEncoder",
    "RunRecord",
    "TikTokConfig",
    "TikTokPublisher",
    "VideoSpec",
    "YouTubeConfig",
    "YouTubePublisher",
    "atomic_video_target",
    "authorize_youtube",
    "build_publisher",
    "mux_audio_track",
    "probe_video",
    "resolve_video_spec",
    "validate_video",
]
