from __future__ import annotations

from dataclasses import dataclass

import pytest

from video_builder_publisher.analytics_platforms import (
    InstagramAnalyticsClient,
    InstagramAnalyticsConfig,
    TikTokAnalyticsClient,
    TikTokAnalyticsConfig,
    YouTubeAnalyticsClient,
    YouTubeAnalyticsConfig,
)


@dataclass
class Response:
    status_code: int
    payload: dict

    def json(self):
        return self.payload


class Session:
    def __init__(self, response: Response) -> None:
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append(("post", args, kwargs))
        return self.response

    def get(self, *args, **kwargs):
        self.calls.append(("get", args, kwargs))
        return self.response


def test_tiktok_normalizes_common_metrics() -> None:
    session = Session(
        Response(
            200,
            {
                "data": {
                    "videos": [
                        {
                            "id": "123",
                            "view_count": 1000,
                            "like_count": 50,
                            "comment_count": 4,
                            "share_count": 8,
                        }
                    ]
                },
                "error": {"code": "ok"},
            },
        )
    )
    client = TikTokAnalyticsClient(TikTokAnalyticsConfig("token"), session=session)
    result = client.fetch_metrics(
        "123",
        published_at="2026-08-25T10:00:00+00:00",
        duration_seconds=20,
        observed_at="2026-08-25T11:00:00+00:00",
    )
    assert result.views == 1000
    assert result.likes == 50
    assert result.comments == 4
    assert result.shares == 8


def test_tiktok_requires_final_numeric_id() -> None:
    client = TikTokAnalyticsClient(TikTokAnalyticsConfig("token"), session=Session(Response(200, {})))
    with pytest.raises(RuntimeError, match="final numeric video id"):
        client.fetch_metrics(
            "draft~abc",
            published_at="2026-08-25T10:00:00+00:00",
            duration_seconds=20,
            observed_at="2026-08-25T11:00:00+00:00",
        )


def test_instagram_normalizes_watch_time_to_seconds() -> None:
    payload = {
        "data": [
            {"name": "views", "values": [{"value": 100}]},
            {"name": "likes", "values": [{"value": 10}]},
            {"name": "ig_reels_avg_watch_time", "values": [{"value": 12500}]},
            {"name": "ig_reels_video_view_total_time", "values": [{"value": 250000}]},
        ]
    }
    client = InstagramAnalyticsClient(
        InstagramAnalyticsConfig("token", "v25.0"),
        session=Session(Response(200, payload)),
    )
    result = client.fetch_metrics(
        "media-id",
        published_at="2026-08-25T10:00:00+00:00",
        duration_seconds=25,
        observed_at="2026-08-25T11:00:00+00:00",
    )
    assert result.views == 100
    assert result.average_view_duration_seconds == 12.5
    assert result.watch_time_seconds == 250
    assert result.average_view_percentage == 50


def test_youtube_combines_fast_and_delayed_sources(monkeypatch, tmp_path) -> None:
    client = YouTubeAnalyticsClient(YouTubeAnalyticsConfig(tmp_path / "token.json"))
    monkeypatch.setattr(
        client,
        "_fetch_fast_counters",
        lambda external_id: {"viewCount": "2000", "likeCount": "100", "commentCount": "9"},
    )
    monkeypatch.setattr(
        client,
        "_fetch_analytics_row",
        lambda external_id, **kwargs: {
            "views": 1800,
            "engagedViews": 1200,
            "shares": 30,
            "estimatedMinutesWatched": 500,
            "averageViewDuration": 18.2,
            "averageViewPercentage": 73.0,
        },
    )
    result = client.fetch_metrics(
        "video",
        published_at="2026-08-25T10:00:00+00:00",
        duration_seconds=25,
        observed_at="2026-08-25T11:00:00+00:00",
    )
    assert result.views == 2000
    assert result.engaged_views == 1200
    assert result.likes == 100
    assert result.comments == 9
    assert result.shares == 30
    assert result.watch_time_seconds == 30000
