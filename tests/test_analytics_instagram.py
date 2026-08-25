from __future__ import annotations

from dataclasses import dataclass

from video_builder_publisher import InstagramAnalyticsClient, InstagramAnalyticsConfig


@dataclass
class Response:
    status_code: int
    payload: dict

    def json(self):
        return self.payload


class Session:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return Response(
                200,
                {
                    "data": [
                        {"name": "views", "values": [{"value": 900}]},
                        {"name": "likes", "values": [{"value": 70}]},
                        {"name": "comments", "values": [{"value": 8}]},
                    ]
                },
            )
        return Response(400, {"error": {"message": "unsupported metric"}})


def test_optional_reel_metrics_do_not_hide_base_metrics() -> None:
    client = InstagramAnalyticsClient(
        InstagramAnalyticsConfig("token"),
        session=Session(),
    )
    snapshot = client.fetch_metrics(
        "media",
        published_at="2026-08-25T10:00:00+00:00",
        duration_seconds=22,
        observed_at="2026-08-25T11:00:00+00:00",
    )
    assert snapshot.status == "ok"
    assert snapshot.views == 900
    assert snapshot.likes == 70
    assert snapshot.comments == 8
    assert snapshot.average_view_duration_seconds is None
