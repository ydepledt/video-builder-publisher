from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from video_builder_publisher.analytics import (
    AnalyticsProfile,
    AnalyticsStore,
    ContentContext,
    MetricSnapshot,
    PublicationAnalyticsSource,
    PublicationAnalyticsTarget,
)


class DemoProfile(AnalyticsProfile):
    def build_context(self, publication, manifest):
        manifest = manifest or {}
        return ContentContext(
            run_key=publication.run_key,
            scope_key=publication.scope_key,
            run_date=publication.run_date,
            title=manifest.get("title"),
            content_format=publication.content_format,
            duration_seconds=manifest.get("duration_seconds"),
            dimensions={"kind": manifest.get("kind"), "score": manifest.get("score")},
        )


def _publication_db(path: Path, artifact: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE runs (
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
            CREATE TABLE publications (
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
            """
            INSERT INTO runs(
                run_key, scope_key, run_date, status, content_format,
                artifact_path, started_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "demo|2026-08-25",
                "demo",
                "2026-08-25",
                "completed",
                "daily",
                str(artifact),
                "2026-08-25T08:00:00+00:00",
            ),
        )
        conn.execute(
            """
            INSERT INTO publications(run_key, platform, status, attempts, external_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "demo|2026-08-25",
                "youtube",
                "published",
                1,
                "video-1",
                "2026-08-25T08:05:00+00:00",
            ),
        )


def test_sync_publications_and_assign_checkpoints(tmp_path: Path) -> None:
    queue = tmp_path / "queue" / "published" / "run"
    queue.mkdir(parents=True)
    artifact = queue / "video.mp4"
    artifact.write_bytes(b"video")
    (queue / "manifest.json").write_text(
        json.dumps({"title": "Demo", "kind": "battle", "score": 42, "duration_seconds": 20}),
        encoding="utf-8",
    )
    publication_db = tmp_path / "publication.sqlite"
    _publication_db(publication_db, artifact)

    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    result = store.sync_publications(PublicationAnalyticsSource(publication_db), DemoProfile())
    assert result == {"publication_rows": 1, "targets_linked": 1, "needs_manual_link": 0}

    target = store.get_target("demo|2026-08-25", "youtube")
    assert target is not None
    assert target.external_id == "video-1"
    assert target.duration_seconds == 20

    published = datetime.fromisoformat(target.published_at)
    observed = published + timedelta(hours=25)
    _, checkpoints = store.insert_snapshot(
        target,
        observed.isoformat(),
        MetricSnapshot(status="ok", views=1000, likes=40, comments=5, shares=10),
    )
    assert checkpoints == [1, 6, 24]

    # A later collection must not rewrite immutable first-successful checkpoints.
    _, checkpoints = store.insert_snapshot(
        target,
        (published + timedelta(hours=30)).isoformat(),
        MetricSnapshot(status="ok", views=1500, likes=50),
    )
    assert checkpoints == []

    report = store.checkpoint_report()
    assert [row["checkpoint_hours"] for row in report] == [1, 6, 24]
    assert report[0]["views"] == 1000
    assert report[0]["dimensions"] == {"kind": "battle", "score": 42}
    assert report[0]["engagement_rate"] == pytest.approx(0.055)


def test_manual_target_wins_over_publication_sync(tmp_path: Path) -> None:
    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    store.upsert_context(ContentContext("run", "scope", "2026-08-25"))
    store.link_target(
        "run",
        "scope",
        "tiktok",
        "12345",
        "2026-08-25T10:00:00+00:00",
        source="manual",
    )
    store.link_target(
        "run",
        "scope",
        "tiktok",
        "wrong",
        "2026-08-25T10:05:00+00:00",
        source="publication_store",
    )
    target = store.get_target("run", "tiktok")
    assert target is not None
    assert target.external_id == "12345"
    assert target.source == "manual"


def test_retention_round_trip(tmp_path: Path) -> None:
    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    store.replace_retention(
        "run",
        "youtube",
        24,
        [
            {
                "elapsed_ratio": 0.5,
                "audience_watch_ratio": 0.8,
                "relative_retention_performance": 0.6,
            }
        ],
    )
    assert store.retention_report(run_key="run") == [
        {
            "run_key": "run",
            "platform": "youtube",
            "checkpoint_hours": 24,
            "elapsed_ratio": 0.5,
            "audience_watch_ratio": 0.8,
            "relative_retention_performance": 0.6,
        }
    ]


def test_publication_source_can_filter_scope(tmp_path: Path) -> None:
    artifact = tmp_path / "video.mp4"
    artifact.write_bytes(b"x")
    db = tmp_path / "publication.sqlite"
    _publication_db(db, artifact)
    rows = PublicationAnalyticsSource(db).targets(scope_key="demo")
    assert len(rows) == 1
    assert isinstance(rows[0], PublicationAnalyticsTarget)
    assert rows[0].scope_key == "demo"


def test_requires_timezone_aware_target(tmp_path: Path) -> None:
    store = AnalyticsStore(tmp_path / "analytics.sqlite")
    with pytest.raises(ValueError, match="timezone-aware"):
        store.link_target("run", "scope", "youtube", "id", "2026-08-25T10:00:00", source="manual")


def test_custom_checkpoint_policy_is_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        AnalyticsStore(tmp_path / "x.sqlite", checkpoint_hours=(0, 1))
    store = AnalyticsStore(tmp_path / "ok.sqlite", checkpoint_hours=(2, 12))
    assert store.checkpoint_hours == (2, 12)
