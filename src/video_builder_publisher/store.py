"""Generic SQLite publication state registry."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from .security import ensure_private_dir, redact_sensitive_text

TERMINAL_SUCCESS = {"published", "submitted", "draft_uploaded"}
NON_RETRYABLE = TERMINAL_SUCCESS | {"unknown"}
ALLOWED_PLATFORM_STATUSES = {
    "prepared",
    "publishing",
    "published",
    "submitted",
    "draft_uploaded",
    "failed",
    "unknown",
}


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_key: str
    scope_key: str
    run_date: str
    status: str
    content_format: str | None
    artifact_path: str | None
    artifact_sha256: str | None


class PublicationStore:
    """SQLite registry preventing duplicate logical-run/platform publications."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def initialize(self) -> None:
        ensure_private_dir(self.path.parent)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
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
                CREATE INDEX IF NOT EXISTS idx_runs_scope_date
                    ON runs(scope_key, run_date DESC);

                CREATE TABLE IF NOT EXISTS publications (
                    run_key TEXT NOT NULL,
                    platform TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    external_id TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_key, platform),
                    FOREIGN KEY (run_key) REFERENCES runs(run_key)
                );
                """
            )

    @staticmethod
    def run_key(scope_key: str, run_date: date) -> str:
        return f"{scope_key}|{run_date.isoformat()}"

    def begin_run(self, scope_key: str, run_date: date, force: bool = False) -> str:
        return self.begin_named_run(self.run_key(scope_key, run_date), scope_key, run_date, force=force)

    def begin_named_run(
        self,
        run_key: str,
        scope_key: str,
        run_date: date,
        *,
        force: bool = False,
    ) -> str:
        """Begin a logical run whose identity is not restricted to one item per day."""

        key = run_key.strip()
        scope = scope_key.strip()
        if not key:
            raise ValueError("run_key cannot be empty")
        if not scope:
            raise ValueError("scope_key cannot be empty")

        self.initialize()
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM runs WHERE run_key = ?", (key,)).fetchone()
            if row and not force and row[0] in {
                "rendered",
                "publishing",
                "partial",
                "needs_review",
                "completed",
            }:
                return key

            if row:
                conn.execute(
                    """
                    UPDATE runs
                    SET scope_key=?, run_date=?, status='running', started_at=?,
                        completed_at=NULL, error=NULL
                    WHERE run_key=?
                    """,
                    (scope, run_date.isoformat(), now, key),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO runs(run_key, scope_key, run_date, status, started_at)
                    VALUES (?, ?, ?, 'running', ?)
                    """,
                    (key, scope, run_date.isoformat(), now),
                )
        return key

    def get_run(self, run_key: str) -> RunRecord | None:
        self.initialize()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT run_key, scope_key, run_date, status, content_format,
                       artifact_path, artifact_sha256
                FROM runs WHERE run_key=?
                """,
                (run_key,),
            ).fetchone()
        return RunRecord(*row) if row else None

    def mark_rendered(
        self,
        run_key: str,
        content_format: str,
        signature: str,
        artifact_path: str,
        artifact_sha256: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status='rendered', content_format=?, selection_signature=?,
                    artifact_path=?, artifact_sha256=?, error=NULL
                WHERE run_key=?
                """,
                (content_format, signature, artifact_path, artifact_sha256, run_key),
            )

    def update_artifact_path(self, run_key: str, artifact_path: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE runs SET artifact_path=? WHERE run_key=?", (artifact_path, run_key))

    def mark_run_failed(self, run_key: str, error: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status='failed', error=?, completed_at=? WHERE run_key=?",
                (redact_sensitive_text(error), _now(), run_key),
            )

    def recover_interrupted_publications(self, run_key: str) -> int:
        self.initialize()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE publications
                SET status='unknown', error=?, updated_at=?
                WHERE run_key=? AND status='publishing'
                """,
                (
                    "Previous process ended while remote publishing was in flight; "
                    "manual/API verification is required before any retry.",
                    _now(),
                    run_key,
                ),
            )
        return int(cursor.rowcount or 0)

    def finalize_run(self, run_key: str) -> str:
        statuses = self.platform_statuses(run_key)
        if not statuses:
            status = "rendered"
        elif any(value == "unknown" for value in statuses.values()):
            status = "needs_review"
        elif all(value in TERMINAL_SUCCESS for value in statuses.values()):
            status = "completed"
        elif any(value == "failed" for value in statuses.values()):
            status = "partial"
        else:
            status = "publishing"

        completed = _now() if status in {"completed", "partial", "needs_review"} else None
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status=?, completed_at=? WHERE run_key=?",
                (status, completed, run_key),
            )
        return status

    def recent_formats(self, scope_key: str, limit: int = 7) -> list[str]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT content_format FROM runs
                WHERE scope_key=? AND content_format IS NOT NULL
                  AND status IN ('publishing', 'partial', 'needs_review', 'completed')
                ORDER BY run_date DESC LIMIT ?
                """,
                (scope_key, limit),
            ).fetchall()
        return [str(row[0]) for row in rows]

    def begin_platform(self, run_key: str, platform: str) -> bool:
        self.initialize()
        now = _now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM publications WHERE run_key=? AND platform=?",
                (run_key, platform),
            ).fetchone()
            if row and row[0] in NON_RETRYABLE:
                return False
            if row:
                conn.execute(
                    """
                    UPDATE publications
                    SET status='publishing', attempts=attempts+1, error=NULL, updated_at=?
                    WHERE run_key=? AND platform=?
                    """,
                    (now, run_key, platform),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO publications(run_key, platform, status, attempts, updated_at)
                    VALUES (?, ?, 'publishing', 1, ?)
                    """,
                    (run_key, platform, now),
                )
        return True

    def record_platform_failure(self, run_key: str, platform: str, error: str) -> None:
        self.initialize()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO publications(run_key, platform, status, attempts, error, updated_at)
                VALUES (?, ?, 'failed', 0, ?, ?)
                ON CONFLICT(run_key, platform) DO UPDATE SET
                    status='failed', error=excluded.error, updated_at=excluded.updated_at
                WHERE publications.status NOT IN
                    ('published', 'submitted', 'draft_uploaded', 'unknown')
                """,
                (run_key, platform, redact_sensitive_text(error), _now()),
            )

    def finish_platform(
        self,
        run_key: str,
        platform: str,
        status: str,
        external_id: str | None = None,
        error: str | None = None,
    ) -> None:
        if status not in ALLOWED_PLATFORM_STATUSES:
            raise ValueError(f"Unsupported platform status: {status}")
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE publications
                SET status=?, external_id=?, error=?, updated_at=?
                WHERE run_key=? AND platform=?
                """,
                (
                    status,
                    external_id,
                    redact_sensitive_text(error) if error else None,
                    _now(),
                    run_key,
                    platform,
                ),
            )

    def platform_statuses(self, run_key: str) -> dict[str, str]:
        self.initialize()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT platform, status FROM publications WHERE run_key=?", (run_key,)
            ).fetchall()
        return {str(platform): str(status) for platform, status in rows}

    def _connect(self) -> sqlite3.Connection:
        ensure_private_dir(self.path.parent)
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
