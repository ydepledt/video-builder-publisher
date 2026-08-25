"""Shared video building, publication and analytics primitives."""

from .analytics import (
    DEFAULT_CHECKPOINT_HOURS,
    AnalyticsProfile,
    AnalyticsStore,
    AnalyticsTarget,
    ContentContext,
    MetricSnapshot,
    PublicationAnalyticsSource,
    PublicationAnalyticsTarget,
)
from .analytics_instagram import InstagramAnalyticsClient
from .analytics_platforms import (
    AnalyticsError,
    InstagramAnalyticsConfig,
    TikTokAnalyticsClient,
    TikTokAnalyticsConfig,
    YouTubeAnalyticsClient,
    YouTubeAnalyticsConfig,
    authorize_youtube_analytics,
)
from .analytics_service import AnalyticsCollector
from .analytics_worker import DEFAULT_ANALYTICS_INTERVAL_MINUTES, run_periodic_analytics
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
    "DEFAULT_ANALYTICS_INTERVAL_MINUTES",
    "DEFAULT_CHECKPOINT_HOURS",
    "VIDEO_PROFILES",
    "AnalyticsCollector",
    "AnalyticsError",
    "AnalyticsProfile",
    "AnalyticsStore",
    "AnalyticsTarget",
    "ContentContext",
    "InstagramAnalyticsClient",
    "InstagramAnalyticsConfig",
    "InstagramConfig",
    "InstagramPublisher",
    "MetricSnapshot",
    "PublicationAnalyticsSource",
    "PublicationAnalyticsTarget",
    "PublishError",
    "PublishQueue",
    "PublishResult",
    "PublicationStore",
    "Publisher",
    "RawVideoEncoder",
    "RunRecord",
    "TikTokAnalyticsClient",
    "TikTokAnalyticsConfig",
    "TikTokConfig",
    "TikTokPublisher",
    "VideoSpec",
    "YouTubeAnalyticsClient",
    "YouTubeAnalyticsConfig",
    "YouTubeConfig",
    "YouTubePublisher",
    "atomic_video_target",
    "authorize_youtube",
    "authorize_youtube_analytics",
    "build_publisher",
    "mux_audio_track",
    "probe_video",
    "resolve_video_spec",
    "run_periodic_analytics",
    "validate_video",
]
