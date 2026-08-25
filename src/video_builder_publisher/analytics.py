"""Shared, platform-neutral analytics primitives for generated videos."""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_CHECKPOINT_HOURS = (1, 6, 24, 168)
TERMINAL_PUBLICATION_STATUSES = ("published", "submitted", "draft_uploaded")


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    status: str
    views: int | None = None
    engaged_views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    saves: int | None = None
    reach: int | None = None
    total_interactions: int | None = None
    watch_time_seconds: float | None = None
    average_view_duration_seconds: float | None = None
    average_view_percentage: float | None = None
    skip_rate: float | None = None
    raw_metrics: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class PublicationAnalyticsTarget:
    run_key: str
    scope_key: str
    run_date: str
    platform: str
    status: str
    external_id: str | None
    published_at: str
    artifact_path: str | None
    content_format: str | None


@dataclass(frozen=True, slots=True)
class AnalyticsTarget:
    run_key: str
    scope_key: str
    platform: str
    external_id: str
    published_at: str
    source: str
    duration_seconds: float | None


@dataclass(frozen=True, slots=True)
class ContentContext:
    run_key: str
    scope_key: str
    run_date: str
    title: str | None = None
    content_format: str | None = None
    duration_seconds: float | None = None
    dimensions: dict[str, Any] | None = None
    manifest_path: str | None = None


class AnalyticsProfile(ABC):
    """Generator-specific manifest → content-dimensions adapter."""

    @abstractmethod
    def build_context(
        self,
        publication: PublicationAnalyticsTarget,
        manifest: dict[str, Any] | None,
    ) -> ContentContext:
        """Return only generator-specific context; common metrics stay shared."""
        raise NotImplementedError


class PublicationAnalyticsSource:
    """Read analytics targets from the generic PublicationStore schema."""

    def __init__(self, publication_db: str | Path) -> None:
        self.path = Path(publication_db)

    def targets(
        self,
        *,
        scope_key: str | None = None,
        run_key: str | None = None,
        platforms: Iterable[str] | None = None,
        statuses: Iterable[str] = TERMINAL_PUBLICATION_STATUSES,
    ) -> list[PublicationAnalyticsTarget]:
        if not self.path.is_file():
            return []
        selected_statuses = frozenset(str(value) for value in statuses)
        selected_platforms = frozenset(str(value) for value in (platforms or ()))
        if not selected_statuses:
            return []

        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=10.0)
            rows = conn.execute(
                """
                SELECT
                    p.run_key, r.scope_key, r.run_date, p.platform, p.status,
                    p.external_id, p.updated_at, r.artifact_path, r.content_format
                FROM publications p
                JOIN runs r ON r.run_key=p.run_key
                ORDER BY p.updated_at DESC
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise RuntimeError("Unable to read publication analytics targets") from exc
        finally:
            if conn is not None:
                conn.close()

        result: list[PublicationAnalyticsTarget] = []
        for row in rows:
            target = PublicationAnalyticsTarget(*row)
            if target.status not in selected_statuses:
                continue
            if scope_key and target.scope_key != scope_key:
                continue
            if run_key and target.run_key != run_key:
                continue
            if selected_platforms and target.platform not in selected_platforms:
                continue
            result.append(target)
        return result

    @staticmethod
    def load_manifest(
        target: PublicationAnalyticsTarget,
    ) -> tuple[dict[str, Any] | None, str | None]:
        if not target.artifact_path:
            return None, None
        path = Path(target.artifact_path).parent / "manifest.json"
        if not path.is_file():
            return None, str(path)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, str(path)
        return (payload if isinstance(payload, dict) else None), str(path)


class AnalyticsStore:
    """Persistent normalized performance history shared by every generator."""

    def __init__(
        self,
        path: str | Path,
        *,
        checkpoint_hours: Iterable[int] = DEFAULT_CHECKPOINT_HOURS,
    ) -> None:
        self.path = Path(path)
        checkpoints = tuple(sorted(dict.fromkeys(int(value) for value in checkpoint_hours)))
        if not checkpoints or any(value <= 0 for value in checkpoints):
            raise ValueError("checkpoint_hours must contain positive integers")
        self.checkpoint_hours = checkpoints

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS content_context (
                    run_key TEXT PRIMARY KEY,
                    scope_key TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    title TEXT,
                    content_format TEXT,
                    duration_seconds REAL,
                    dimensions_json TEXT NOT NULL DEFAULT '{}',
                    manifest_path TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_context_scope_date
                    ON content_context(scope_key, run_date DESC);

                CREATE TABLE IF NOT EXISTS targets (
                    run_key TEXT NOT NULL,
                    scope_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    linked_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, platform)
                );
                CREATE INDEX IF NOT EXISTS idx_targets_published_at
                    ON targets(published_at DESC);

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    age_seconds INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    views INTEGER,
                    engaged_views INTEGER,
                    likes INTEGER,
                    comments INTEGER,
                    shares INTEGER,
                    saves INTEGER,
                    reach INTEGER,
                    total_interactions INTEGER,
                    watch_time_seconds REAL,
                    average_view_duration_seconds REAL,
                    average_view_percentage REAL,
                    skip_rate REAL,
                    raw_metrics_json TEXT NOT NULL DEFAULT '{}',
                    UNIQUE (run_key, platform, observed_at),
                    FOREIGN KEY (run_key, platform)
                        REFERENCES targets(run_key, platform)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_run_platform_age
                    ON snapshots(run_key, platform, age_seconds);

                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    checkpoint_hours INTEGER NOT NULL,
                    snapshot_id INTEGER NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, platform, checkpoint_hours),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
                );

                CREATE TABLE IF NOT EXISTS retention_points (
                    run_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    checkpoint_hours INTEGER NOT NULL,
                    elapsed_ratio REAL NOT NULL,
                    audience_watch_ratio REAL,
                    relative_retention_performance REAL,
                    PRIMARY KEY (run_key, platform, checkpoint_hours, elapsed_ratio)
                );

                CREATE TABLE IF NOT EXISTS collection_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    eligible_targets INTEGER NOT NULL DEFAULT 0,
                    collected INTEGER NOT NULL DEFAULT 0,
                    no_data INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    error_summary_json TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def upsert_context(self, context: ContentContext) -> None:
        self.initialize()
        dimensions = json.dumps(context.dimensions or {}, sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO content_context(
                    run_key, scope_key, run_date, title, content_format,
                    duration_seconds, dimensions_json, manifest_path, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    scope_key=excluded.scope_key,
                    run_date=excluded.run_date,
                    title=excluded.title,
                    content_format=excluded.content_format,
                    duration_seconds=COALESCE(excluded.duration_seconds, content_context.duration_seconds),
                    dimensions_json=excluded.dimensions_json,
                    manifest_path=COALESCE(excluded.manifest_path, content_context.manifest_path),
                    updated_at=excluded.updated_at
                """,
                (
                    context.run_key,
                    context.scope_key,
                    context.run_date,
                    context.title,
                    context.content_format,
                    context.duration_seconds,
                    dimensions,
                    context.manifest_path,
                    _now(),
                ),
            )

    def link_target(
        self,
        run_key: str,
        scope_key: str,
        platform: str,
        external_id: str,
        published_at: str,
        *,
        source: str,
        force: bool = False,
    ) -> None:
        self.initialize()
        normalized_time = _aware_timestamp(published_at)
        if not external_id.strip():
            raise ValueError("analytics external_id cannot be empty")
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT external_id, published_at, source FROM targets WHERE run_key=? AND platform=?",
                (run_key, platform),
            ).fetchone()
            if existing and not force:
                existing_id, existing_published_at, existing_source = map(str, existing)
                if existing_source == "manual":
                    return
                if existing_id == external_id and existing_published_at == normalized_time:
                    return
            conn.execute(
                """
                INSERT INTO targets(run_key, scope_key, platform, external_id, published_at, source, linked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key, platform) DO UPDATE SET
                    scope_key=excluded.scope_key,
                    external_id=excluded.external_id,
                    published_at=excluded.published_at,
                    source=excluded.source,
                    linked_at=excluded.linked_at
                """,
                (run_key, scope_key, platform, external_id, normalized_time, source, _now()),
            )

    def sync_publications(
        self,
        source: PublicationAnalyticsSource,
        profile: AnalyticsProfile,
        *,
        scope_key: str | None = None,
        run_key: str | None = None,
        platforms: Iterable[str] | None = None,
        requires_manual_link: Callable[[PublicationAnalyticsTarget], bool] | None = None,
    ) -> dict[str, int]:
        publications = source.targets(scope_key=scope_key, run_key=run_key, platforms=platforms)
        linked = 0
        manual = 0
        for publication in publications:
            manifest, manifest_path = source.load_manifest(publication)
            context = profile.build_context(publication, manifest)
            if context.manifest_path is None and manifest_path:
                context = replace(context, manifest_path=manifest_path)
            self.upsert_context(context)
            remote_id = str(publication.external_id or "").strip()
            if not remote_id or (requires_manual_link and requires_manual_link(publication)):
                manual += 1
                continue
            self.link_target(
                publication.run_key,
                publication.scope_key,
                publication.platform,
                remote_id,
                publication.published_at,
                source="publication_store",
            )
            linked += 1
        return {
            "publication_rows": len(publications),
            "targets_linked": linked,
            "needs_manual_link": manual,
        }

    def get_target(self, run_key: str, platform: str) -> AnalyticsTarget | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT t.run_key, t.scope_key, t.platform, t.external_id, t.published_at,
                       t.source, c.duration_seconds
                FROM targets t LEFT JOIN content_context c ON c.run_key=t.run_key
                WHERE t.run_key=? AND t.platform=?
                """,
                (run_key, platform),
            ).fetchone()
        return AnalyticsTarget(*row) if row else None

    def targets(
        self,
        *,
        run_key: str | None = None,
        scope_key: str | None = None,
        platforms: Iterable[str] | None = None,
    ) -> list[AnalyticsTarget]:
        self.initialize()
        selected_platforms = frozenset(str(value) for value in (platforms or ()))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.run_key, t.scope_key, t.platform, t.external_id, t.published_at,
                       t.source, c.duration_seconds
                FROM targets t LEFT JOIN content_context c ON c.run_key=t.run_key
                ORDER BY t.published_at DESC, t.platform
                """
            ).fetchall()
        result: list[AnalyticsTarget] = []
        for row in rows:
            target = AnalyticsTarget(*row)
            if run_key and target.run_key != run_key:
                continue
            if scope_key and target.scope_key != scope_key:
                continue
            if selected_platforms and target.platform not in selected_platforms:
                continue
            result.append(target)
        return result

    def insert_snapshot(
        self,
        target: AnalyticsTarget,
        observed_at: str,
        snapshot: MetricSnapshot,
    ) -> tuple[int, list[int]]:
        self.initialize()
        normalized_observed = _aware_timestamp(observed_at)
        observed = _parse_aware(normalized_observed)
        published = _parse_aware(target.published_at)
        age_seconds = max(0, int((observed - published).total_seconds()))
        raw = json.dumps(snapshot.raw_metrics or {}, sort_keys=True, separators=(",", ":"))
        metrics = (
            snapshot.views,
            snapshot.engaged_views,
            snapshot.likes,
            snapshot.comments,
            snapshot.shares,
            snapshot.saves,
            snapshot.reach,
            snapshot.total_interactions,
            snapshot.watch_time_seconds,
            snapshot.average_view_duration_seconds,
            snapshot.average_view_percentage,
            snapshot.skip_rate,
        )
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO snapshots(
                    run_key, platform, external_id, observed_at, age_seconds, status,
                    views, engaged_views, likes, comments, shares, saves, reach,
                    total_interactions, watch_time_seconds, average_view_duration_seconds,
                    average_view_percentage, skip_rate, raw_metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_key, platform, observed_at) DO UPDATE SET
                    external_id=excluded.external_id,
                    age_seconds=excluded.age_seconds,
                    status=excluded.status,
                    views=excluded.views,
                    engaged_views=excluded.engaged_views,
                    likes=excluded.likes,
                    comments=excluded.comments,
                    shares=excluded.shares,
                    saves=excluded.saves,
                    reach=excluded.reach,
                    total_interactions=excluded.total_interactions,
                    watch_time_seconds=excluded.watch_time_seconds,
                    average_view_duration_seconds=excluded.average_view_duration_seconds,
                    average_view_percentage=excluded.average_view_percentage,
                    skip_rate=excluded.skip_rate,
                    raw_metrics_json=excluded.raw_metrics_json
                """,
                (
                    target.run_key,
                    target.platform,
                    target.external_id,
                    normalized_observed,
                    age_seconds,
                    snapshot.status,
                    *metrics,
                    raw,
                ),
            )
            row = conn.execute(
                "SELECT id FROM snapshots WHERE run_key=? AND platform=? AND observed_at=?",
                (target.run_key, target.platform, normalized_observed),
            ).fetchone()
            if row is None:
                raise RuntimeError("analytics snapshot insert could not be read back")
            snapshot_id = int(row[0])
            assigned: list[int] = []
            if snapshot.status == "ok":
                for checkpoint in self.checkpoint_hours:
                    if age_seconds < checkpoint * 3600:
                        continue
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO checkpoints(
                            run_key, platform, checkpoint_hours, snapshot_id, assigned_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (target.run_key, target.platform, checkpoint, snapshot_id, _now()),
                    )
                    if cursor.rowcount:
                        assigned.append(checkpoint)
        return snapshot_id, assigned

    def replace_retention(
        self,
        run_key: str,
        platform: str,
        checkpoint_hours: int,
        points: list[dict[str, float | None]],
    ) -> None:
        self.initialize()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM retention_points WHERE run_key=? AND platform=? AND checkpoint_hours=?",
                (run_key, platform, checkpoint_hours),
            )
            conn.executemany(
                """
                INSERT INTO retention_points(
                    run_key, platform, checkpoint_hours, elapsed_ratio,
                    audience_watch_ratio, relative_retention_performance
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_key,
                        platform,
                        checkpoint_hours,
                        float(point["elapsed_ratio"]),
                        _float(point.get("audience_watch_ratio")),
                        _float(point.get("relative_retention_performance")),
                    )
                    for point in points
                    if point.get("elapsed_ratio") is not None
                ],
            )

    def start_collection_run(self) -> int:
        self.initialize()
        with self._connect() as conn:
            cursor = conn.execute("INSERT INTO collection_runs(started_at) VALUES (?)", (_now(),))
            return int(cursor.lastrowid)

    def finish_collection_run(
        self,
        collection_id: int,
        *,
        eligible_targets: int,
        collected: int,
        no_data: int,
        skipped: int,
        errors: dict[str, str],
    ) -> None:
        self.initialize()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE collection_runs SET completed_at=?, eligible_targets=?, collected=?,
                    no_data=?, skipped=?, errors=?, error_summary_json=? WHERE id=?
                """,
                (
                    _now(),
                    eligible_targets,
                    collected,
                    no_data,
                    skipped,
                    len(errors),
                    json.dumps(errors, sort_keys=True),
                    collection_id,
                ),
            )

    def checkpoint_report(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT cp.run_key, c.scope_key, cp.platform, cp.checkpoint_hours,
                       s.observed_at, s.age_seconds, s.views, s.engaged_views, s.likes,
                       s.comments, s.shares, s.saves, s.reach, s.total_interactions,
                       s.watch_time_seconds, s.average_view_duration_seconds,
                       s.average_view_percentage, s.skip_rate, c.run_date, c.title,
                       c.content_format, c.duration_seconds, c.dimensions_json
                FROM checkpoints cp
                JOIN snapshots s ON s.id=cp.snapshot_id
                LEFT JOIN content_context c ON c.run_key=cp.run_key
                ORDER BY c.run_date DESC, cp.platform, cp.checkpoint_hours
                """
            ).fetchall()
        return [self._report_row(row) for row in rows]

    def latest_report(self) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT s.*, ROW_NUMBER() OVER (
                        PARTITION BY s.run_key, s.platform ORDER BY s.observed_at DESC
                    ) AS rn FROM snapshots s WHERE s.status='ok'
                )
                SELECT r.run_key, c.scope_key, r.platform, NULL,
                       r.observed_at, r.age_seconds, r.views, r.engaged_views, r.likes,
                       r.comments, r.shares, r.saves, r.reach, r.total_interactions,
                       r.watch_time_seconds, r.average_view_duration_seconds,
                       r.average_view_percentage, r.skip_rate, c.run_date, c.title,
                       c.content_format, c.duration_seconds, c.dimensions_json
                FROM ranked r LEFT JOIN content_context c ON c.run_key=r.run_key
                WHERE r.rn=1 ORDER BY c.run_date DESC, r.platform
                """
            ).fetchall()
        return [self._report_row(row) for row in rows]

    @staticmethod
    def _report_row(row: tuple[Any, ...]) -> dict[str, Any]:
        keys = (
            "run_key",
            "scope_key",
            "platform",
            "checkpoint_hours",
            "observed_at",
            "age_seconds",
            "views",
            "engaged_views",
            "likes",
            "comments",
            "shares",
            "saves",
            "reach",
            "total_interactions",
            "watch_time_seconds",
            "average_view_duration_seconds",
            "average_view_percentage",
            "skip_rate",
            "run_date",
            "title",
            "content_format",
            "duration_seconds",
            "dimensions_json",
        )
        data = dict(zip(keys, row, strict=True))
        data["observed_age_hours"] = round(int(data.pop("age_seconds") or 0) / 3600, 3)
        data["dimensions"] = json.loads(data.pop("dimensions_json") or "{}")
        actions = sum(
            int(data.get(name) or 0) for name in ("likes", "comments", "shares", "saves")
        )
        views = data.get("views")
        data["engagement_rate"] = (
            actions / int(views) if views is not None and int(views) > 0 else None
        )
        return data

    def retention_report(
        self,
        *,
        run_key: str | None = None,
        platform: str = "youtube",
    ) -> list[dict[str, Any]]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_key, platform, checkpoint_hours, elapsed_ratio,
                       audience_watch_ratio, relative_retention_performance
                FROM retention_points
                WHERE platform=? AND (? IS NULL OR run_key=?)
                ORDER BY run_key DESC, checkpoint_hours, elapsed_ratio
                """,
                (platform, run_key, run_key),
            ).fetchall()
        keys = (
            "run_key",
            "platform",
            "checkpoint_hours",
            "elapsed_ratio",
            "audience_watch_ratio",
            "relative_retention_performance",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def _aware_timestamp(value: str) -> str:
    return _parse_aware(value).isoformat(timespec="seconds")


def _float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
