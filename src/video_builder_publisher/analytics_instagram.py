"""Capability-tolerant Instagram analytics client."""

from __future__ import annotations

from typing import Any

from .analytics import MetricSnapshot
from .analytics_platforms import (
    InstagramAnalyticsClient as _BaseInstagramAnalyticsClient,
    _float,
    _instagram_metric_value,
    _int,
    _response_json,
)

_BASE_METRICS = "views,reach,likes,comments,shares,saved,total_interactions"
_REEL_METRICS = "ig_reels_avg_watch_time,ig_reels_video_view_total_time,reels_skip_rate"


class InstagramAnalyticsClient(_BaseInstagramAnalyticsClient):
    """Always preserve base insights when optional Reel metrics are unavailable."""

    def fetch_metrics(
        self,
        external_id: str,
        *,
        published_at: str,
        duration_seconds: float | None,
        observed_at: str,
    ) -> MetricSnapshot:
        del published_at, observed_at
        values = self._fetch_values(external_id, _BASE_METRICS)
        values.update(self._fetch_values(external_id, _REEL_METRICS, optional=True))
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
            watch_time_seconds=(
                total_watch_ms / 1000.0 if total_watch_ms is not None else None
            ),
            average_view_duration_seconds=avg_watch_seconds,
            average_view_percentage=average_percentage,
            skip_rate=_float(values.get("reels_skip_rate")),
            raw_metrics=values,
        )

    def _fetch_values(
        self,
        external_id: str,
        metrics: str,
        *,
        optional: bool = False,
    ) -> dict[str, Any]:
        response = self.session.get(
            f"https://graph.instagram.com/{self.config.api_version}/{external_id}/insights",
            params={"metric": metrics},
            headers={"Authorization": f"Bearer {self.config.access_token}"},
            timeout=30,
        )
        if optional and response.status_code == 400:
            return {}
        payload = _response_json(response, "Instagram")
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Instagram insights returned an invalid data payload")
        values: dict[str, Any] = {}
        for item in data:
            if isinstance(item, dict) and item.get("name"):
                values[str(item["name"])] = _instagram_metric_value(item)
        return values
