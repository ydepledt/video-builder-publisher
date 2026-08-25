"""Platform analytics adapters shared by generated-video projects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .analytics import MetricSnapshot
from .security import assert_private_file, atomic_write_text

YOUTUBE_ANALYTICS_SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
_TIKTOK_VIDEO_QUERY = "https://open.tiktokapis.com/v2/video/query/"


class AnalyticsError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class YouTubeAnalyticsConfig:
    token_file: Path


@dataclass(frozen=True, slots=True)
class TikTokAnalyticsConfig:
    access_token: str

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("TikTok analytics access token cannot be empty")


@dataclass(frozen=True, slots=True)
class InstagramAnalyticsConfig:
    access_token: str
    api_version: str = "v25.0"

    def __post_init__(self) -> None:
        if not self.access_token.strip():
            raise ValueError("Instagram analytics access token cannot be empty")
        if not self.api_version.startswith("v"):
            raise ValueError("Instagram api_version must look like v25.0")


class YouTubeAnalyticsClient:
    platform = "youtube"

    def __init__(self, config: YouTubeAnalyticsConfig) -> None:
        self.config = config
        self._analytics = None
        self._data_api = None

    def fetch_metrics(
        self,
        external_id: str,
        *,
        published_at: str,
        duration_seconds: float | None,
        observed_at: str,
    ) -> MetricSnapshot:
        del duration_seconds
        data_row = self._fetch_fast_counters(external_id)
        analytics_row = self._fetch_analytics_row(
            external_id,
            published_at=published_at,
            observed_at=observed_at,
        )
        if data_row is None and analytics_row is None:
            return MetricSnapshot(status="no_data", raw_metrics={})

        analytics_row = analytics_row or {}
        data_row = data_row or {}
        watch_minutes = _float(analytics_row.get("estimatedMinutesWatched"))
        raw = {"data_api": data_row, "analytics_api": analytics_row}
        return MetricSnapshot(
            status="ok",
            views=_int(data_row.get("viewCount") or analytics_row.get("views")),
            engaged_views=_int(analytics_row.get("engagedViews")),
            likes=_int(data_row.get("likeCount") or analytics_row.get("likes")),
            comments=_int(data_row.get("commentCount") or analytics_row.get("comments")),
            shares=_int(analytics_row.get("shares")),
            watch_time_seconds=watch_minutes * 60.0 if watch_minutes is not None else None,
            average_view_duration_seconds=_float(analytics_row.get("averageViewDuration")),
            average_view_percentage=_float(analytics_row.get("averageViewPercentage")),
            raw_metrics=raw,
        )

    def fetch_retention(
        self,
        external_id: str,
        *,
        published_at: str,
        observed_at: str,
    ) -> list[dict[str, float | None]]:
        service = self._youtube_analytics()
        try:
            result = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=_date(published_at),
                    endDate=_date(observed_at),
                    metrics="audienceWatchRatio,relativeRetentionPerformance",
                    dimensions="elapsedVideoTimeRatio",
                    filters=f"video=={external_id}",
                    sort="elapsedVideoTimeRatio",
                )
                .execute()
            )
        except Exception as exc:
            raise AnalyticsError(f"YouTube retention query failed: {type(exc).__name__}") from exc

        headers = _youtube_headers(result)
        points: list[dict[str, float | None]] = []
        for raw_row in result.get("rows") or []:
            if not isinstance(raw_row, list) or len(raw_row) != len(headers):
                continue
            row = dict(zip(headers, raw_row, strict=True))
            elapsed = _float(row.get("elapsedVideoTimeRatio"))
            if elapsed is None:
                continue
            points.append(
                {
                    "elapsed_ratio": elapsed,
                    "audience_watch_ratio": _float(row.get("audienceWatchRatio")),
                    "relative_retention_performance": _float(
                        row.get("relativeRetentionPerformance")
                    ),
                }
            )
        return points

    def _fetch_fast_counters(self, external_id: str) -> dict[str, Any] | None:
        service = self._youtube_data_api()
        try:
            result = service.videos().list(part="statistics", id=external_id).execute()
        except Exception as exc:
            raise AnalyticsError(f"YouTube Data API query failed: {type(exc).__name__}") from exc
        items = result.get("items") or []
        if not items:
            return None
        statistics = items[0].get("statistics") if isinstance(items[0], dict) else None
        return statistics if isinstance(statistics, dict) else None

    def _fetch_analytics_row(
        self,
        external_id: str,
        *,
        published_at: str,
        observed_at: str,
    ) -> dict[str, Any] | None:
        service = self._youtube_analytics()
        try:
            result = (
                service.reports()
                .query(
                    ids="channel==MINE",
                    startDate=_date(published_at),
                    endDate=_date(observed_at),
                    metrics=(
                        "views,engagedViews,likes,comments,shares,estimatedMinutesWatched,"
                        "averageViewDuration,averageViewPercentage"
                    ),
                    filters=f"video=={external_id}",
                )
                .execute()
            )
        except Exception as exc:
            raise AnalyticsError(f"YouTube Analytics query failed: {type(exc).__name__}") from exc
        rows = result.get("rows") or []
        if not rows:
            return None
        headers = _youtube_headers(result)
        row = rows[0]
        if not isinstance(row, list) or len(row) != len(headers):
            raise AnalyticsError("YouTube Analytics returned an invalid row")
        return dict(zip(headers, row, strict=True))

    def _credentials(self):
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise AnalyticsError("Install video-builder-publisher[youtube] for YouTube analytics") from exc

        token_file = assert_private_file(self.config.token_file, "YouTube OAuth token file")
        try:
            credentials = Credentials.from_authorized_user_file(
                str(token_file),
                scopes=list(YOUTUBE_ANALYTICS_SCOPES),
            )
        except (OSError, ValueError) as exc:
            raise AnalyticsError("YouTube analytics token file is invalid") from exc
        if not credentials.has_scopes(YOUTUBE_ANALYTICS_SCOPES):
            raise AnalyticsError("YouTube token lacks read/analytics scopes")
        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise AnalyticsError(
                    f"YouTube analytics OAuth refresh failed: {type(exc).__name__}"
                ) from exc
            atomic_write_text(token_file, credentials.to_json())
        if not credentials.valid:
            raise AnalyticsError("YouTube analytics OAuth credentials are invalid")
        return credentials

    def _youtube_analytics(self):
        if self._analytics is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise AnalyticsError("Install video-builder-publisher[youtube]") from exc
            self._analytics = build(
                "youtubeAnalytics",
                "v2",
                credentials=self._credentials(),
                cache_discovery=False,
            )
        return self._analytics

    def _youtube_data_api(self):
        if self._data_api is None:
            try:
                from googleapiclient.discovery import build
            except ImportError as exc:
                raise AnalyticsError("Install video-builder-publisher[youtube]") from exc
            self._data_api = build(
                "youtube",
                "v3",
                credentials=self._credentials(),
                cache_discovery=False,
            )
        return self._data_api


class TikTokAnalyticsClient:
    platform = "tiktok"

    def __init__(
        self,
        config: TikTokAnalyticsConfig,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    def fetch_metrics(
        self,
        external_id: str,
        *,
        published_at: str,
        duration_seconds: float | None,
        observed_at: str,
    ) -> MetricSnapshot:
        del published_at, duration_seconds, observed_at
        if not external_id.isdigit():
            raise AnalyticsError("TikTok analytics requires the final numeric video id")
        response = self.session.post(
            _TIKTOK_VIDEO_QUERY,
            params={"fields": "id,like_count,comment_count,share_count,view_count"},
            headers={
                "Authorization": f"Bearer {self.config.access_token}",
                "Content-Type": "application/json",
            },
            json={"filters": {"video_ids": [external_id]}},
            timeout=30,
        )
        payload = _response_json(response, "TikTok")
        error = payload.get("error") or {}
        if isinstance(error, dict) and error.get("code") not in {None, "ok", 0}:
            raise AnalyticsError(f"TikTok analytics API error: {error.get('code')}")
        data = payload.get("data") or {}
        videos = data.get("videos") if isinstance(data, dict) else None
        if not isinstance(videos, list):
            raise AnalyticsError("TikTok analytics returned an invalid videos payload")
        video = next(
            (
                item
                for item in videos
                if isinstance(item, dict) and str(item.get("id")) == external_id
            ),
            None,
        )
        if video is None:
            return MetricSnapshot(status="no_data", raw_metrics={})
        safe = {
            key: video.get(key)
            for key in ("id", "view_count", "like_count", "comment_count", "share_count")
        }
        return MetricSnapshot(
            status="ok",
            views=_int(video.get("view_count")),
            likes=_int(video.get("like_count")),
            comments=_int(video.get("comment_count")),
            shares=_int(video.get("share_count")),
            raw_metrics=safe,
        )


class InstagramAnalyticsClient:
    platform = "instagram"

    def __init__(
        self,
        config: InstagramAnalyticsConfig,
        *,
        session: requests.Session | None = None,
    ) -> None:
        self.config = config
        self.session = session or requests.Session()

    def fetch_metrics(
        self,
        external_id: str,
        *,
        published_at: str,
        duration_seconds: float | None,
        observed_at: str,
    ) -> MetricSnapshot:
        del published_at, observed_at
        metrics = (
            "views,reach,likes,comments,shares,saved,total_interactions,"
            "ig_reels_avg_watch_time,ig_reels_video_view_total_time,reels_skip_rate"
        )
        response = self.session.get(
            f"https://graph.instagram.com/{self.config.api_version}/{external_id}/insights",
            params={"metric": metrics},
            headers={"Authorization": f"Bearer {self.config.access_token}"},
            timeout=30,
        )
        payload = _response_json(response, "Instagram")
        data = payload.get("data")
        if not isinstance(data, list):
            raise AnalyticsError("Instagram insights returned an invalid data payload")
        values: dict[str, Any] = {}
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                values[str(item["name"])] = _instagram_metric_value(item)
        if not values:
            return MetricSnapshot(status="no_data", raw_metrics={})

        avg_watch_ms = _float(values.get("ig_reels_avg_watch_time"))
        total_watch_ms = _float(values.get("ig_reels_video_view_total_time"))
        avg_watch_seconds = avg_watch_ms / 1000.0 if avg_watch_ms is not None else None
        average_percentage = None
        if avg_watch_seconds is not None and duration_seconds and duration_seconds > 0:
            average_percentage = avg_watch_seconds / duration_seconds * 100.0
        return MetricSnapshot(
            status="ok",
            views=_int(values.get("views")),
            likes=_int(values.get("likes")),
            comments=_int(values.get("comments")),
            shares=_int(values.get("shares")),
            saves=_int(values.get("saved")),
            reach=_int(values.get("reach")),
            total_interactions=_int(values.get("total_interactions")),
            watch_time_seconds=total_watch_ms / 1000.0 if total_watch_ms is not None else None,
            average_view_duration_seconds=avg_watch_seconds,
            average_view_percentage=average_percentage,
            skip_rate=_float(values.get("reels_skip_rate")),
            raw_metrics=values,
        )


def authorize_youtube_analytics(
    client_secrets_file: str | Path,
    token_file: str | Path,
) -> Path:
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise AnalyticsError("Install video-builder-publisher[youtube]") from exc
    source = assert_private_file(client_secrets_file, "YouTube OAuth client secrets file")
    destination = Path(token_file).expanduser()
    flow = InstalledAppFlow.from_client_secrets_file(str(source), list(YOUTUBE_ANALYTICS_SCOPES))
    credentials = flow.run_local_server(port=0, open_browser=True)
    atomic_write_text(destination, credentials.to_json())
    return destination


def _youtube_headers(payload: dict[str, Any]) -> list[str]:
    return [
        str(item.get("name"))
        for item in (payload.get("columnHeaders") or [])
        if isinstance(item, dict) and item.get("name")
    ]


def _date(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise AnalyticsError(f"Timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC).date().isoformat()


def _response_json(response: requests.Response, platform: str) -> dict[str, Any]:
    if response.status_code in {408, 429} or response.status_code >= 500:
        raise AnalyticsError(
            f"{platform} analytics transient HTTP {response.status_code}",
            retryable=True,
        )
    if response.status_code >= 400:
        raise AnalyticsError(f"{platform} analytics HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise AnalyticsError(f"{platform} analytics returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise AnalyticsError(f"{platform} analytics returned a non-object payload")
    return payload


def _instagram_metric_value(item: dict[str, Any]) -> Any:
    total = item.get("total_value")
    if isinstance(total, dict) and "value" in total:
        return total.get("value")
    values = item.get("values")
    if isinstance(values, list) and values and isinstance(values[-1], dict):
        return values[-1].get("value")
    return item.get("value")


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
