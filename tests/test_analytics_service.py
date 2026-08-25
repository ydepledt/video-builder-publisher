from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from video_builder_publisher import (
    AnalyticsCollector,
    AnalyticsProfile,
    AnalyticsStore,
    ContentContext,
    MetricSnapshot,
    PublicationAnalyticsSource,
)


class Profile(AnalyticsProfile):
    def build_context(self, publication, manifest):
        manifest = manifest or {}
        return ContentContext(
            publication.run_key,
            publication.scope_key,
            publication.run_date,
            title=manifest.get("title"),
            content_format=publication.content_format,
            duration_seconds=20,
            dimensions={"kind": manifest.get("kind")},
        )


class Client:
    platform = "youtube"

    def fetch_metrics(self, *args, **kwargs):
        return MetricSnapshot(status="ok", views=100, likes=10, comments=2, shares=3)

    def fetch_retention(self, *args, **kwargs):
        return [{"elapsed_ratio": 0.5, "audience_watch_ratio": 0.7}]


def _publication(path: Path, artifact: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs(
                run_key TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL,
                content_format TEXT,
                selection_signature TEXT,
                artifact_path TEXT,
                artifact_sha256 TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            );
            CREATE TABLE publications(
                run_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                external_id TEXT,
                error TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_key, platform)
            );
            """
        )
        conn.execute(
            "INSERT INTO runs(run_key,scope_key,run_date,status,content_format,artifact_path,started_at) VALUES(?,?,?,?,?,?,?)",
            (
                "scope|2026-08-25",
                "scope",
                "2026-08-25",
                "completed",
                "daily",
                str(artifact),
                "2026-08-25T09:00:00+00:00",
            ),
        )
        conn.execute(
            "INSERT INTO publications(run_key,platform,status,attempts,external_id,updated_at) VALUES(?,?,?,?,?,?)",
            (
                "scope|2026-08-25",
                "youtube",
                "published",
                1,
                "remote-id",
                "2026-08-25T10:00:00+00:00",
            ),
        )


def test_collector_owns_sync_collect_checkpoints_and_retention(tmp_path: Path) -> None:
    folder = tmp_path / "queue" / "published" / "run"
    folder.mkdir(parents=True)
    artifact = folder / "video.mp4"
    artifact.write_bytes(b"video")
    (folder / "manifest.json").write_text(
        json.dumps({"title": "Example", "kind": "battle"}),
        encoding="utf-8",
    )
    publication_db = tmp_path / "publication.sqlite"
    _publication(publication_db, artifact)
    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    collector = AnalyticsCollector(
        store=store,
        publication_source=PublicationAnalyticsSource(publication_db),
        profile=Profile(),
        scope_key="scope",
        supported_platforms=("youtube",),
        client_factory=lambda platform: Client(),
        is_configured=lambda platform: True,
    )

    observed = datetime(2026, 8, 26, 10, 5, tzinfo=UTC)
    result = collector.collect(observed_at=observed.isoformat())
    assert result["format"] == "video.analytics.collection.v1"
    assert result["collected"] == 1
    assert result["sync"]["targets_linked"] == 1
    assert [row["checkpoint_hours"] for row in collector.report()] == [1, 6, 24]
    retention = collector.report("retention")
    assert len(retention) == 1
    assert retention[0]["checkpoint_hours"] == 24


def test_collector_manual_link_uses_publication_timestamp(tmp_path: Path) -> None:
    folder = tmp_path / "queue" / "submitted" / "run"
    folder.mkdir(parents=True)
    artifact = folder / "video.mp4"
    artifact.write_bytes(b"x")
    publication_db = tmp_path / "publication.sqlite"
    _publication(publication_db, artifact)
    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    collector = AnalyticsCollector(
        store=store,
        publication_source=PublicationAnalyticsSource(publication_db),
        profile=Profile(),
        scope_key="scope",
        supported_platforms=("youtube",),
        client_factory=lambda platform: Client(),
        is_configured=lambda platform: True,
        requires_manual_link=lambda publication: True,
    )
    result = collector.link_target("scope|2026-08-25", "youtube", "final-id")
    assert result["published_at"] == "2026-08-25T10:00:00+00:00"
    assert store.get_target("scope|2026-08-25", "youtube").external_id == "final-id"


def test_collector_skips_unconfigured_platform(tmp_path: Path) -> None:
    folder = tmp_path / "queue" / "published" / "run"
    folder.mkdir(parents=True)
    artifact = folder / "video.mp4"
    artifact.write_bytes(b"x")
    db = tmp_path / "publication.sqlite"
    _publication(db, artifact)
    collector = AnalyticsCollector(
        store=AnalyticsStore(tmp_path / "analytics.sqlite"),
        publication_source=PublicationAnalyticsSource(db),
        profile=Profile(),
        scope_key="scope",
        supported_platforms=("youtube",),
        client_factory=lambda platform: Client(),
        is_configured=lambda platform: False,
    )
    observed = datetime(2026, 8, 25, 11, 5, tzinfo=UTC)
    result = collector.collect(observed_at=observed.isoformat())
    assert result["collected"] == 0
    assert result["skipped"] == 1
    assert result["errors"]
