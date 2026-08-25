"""Generator-agnostic staging and publication orchestration."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from .publishing import PublishError, Publisher
from .queue import PublishQueue
from .security import FileLock, sha256_file
from .store import PublicationStore

PublisherFactory = Callable[[str], Publisher]


@dataclass(frozen=True, slots=True)
class PublicationArtifact:
    """One immutable generated video ready to enter the publication queue."""

    run_key: str
    scope_key: str
    run_date: date
    content_format: str
    selection_signature: str
    video_path: Path
    manifest: dict

    def __post_init__(self) -> None:
        if not self.run_key.strip():
            raise ValueError("run_key cannot be empty")
        if not self.scope_key.strip():
            raise ValueError("scope_key cannot be empty")
        if not self.content_format.strip():
            raise ValueError("content_format cannot be empty")
        if not self.selection_signature.strip():
            raise ValueError("selection_signature cannot be empty")


class PublicationService:
    """Shared queue/store state machine for every generated-video application."""

    def __init__(
        self,
        *,
        store: PublicationStore,
        queue: PublishQueue,
        publisher_factory: PublisherFactory,
        supported_platforms: Iterable[str] = ("youtube", "tiktok", "instagram"),
        lock_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.queue = queue
        self.publisher_factory = publisher_factory
        self.supported_platforms = tuple(dict.fromkeys(supported_platforms))
        self.lock_path = Path(lock_path) if lock_path else self.store.path.with_name("publisher.lock")

    def stage(
        self,
        artifact: PublicationArtifact,
        *,
        target_state: str = "needs_review",
    ) -> dict:
        """Idempotently stage one immutable artifact under an arbitrary logical run key."""

        if target_state not in {"ready", "needs_review"}:
            raise ValueError("target_state must be ready or needs_review")
        source = Path(artifact.video_path)
        if not source.is_file() or source.stat().st_size <= 0:
            raise RuntimeError(f"Cannot stage missing/empty artifact: {source}")

        digest = sha256_file(source)
        existing = self.queue.locate(artifact.run_key, expected_sha256=digest)
        if existing is None:
            queued, staged_digest = self.queue.stage(
                source,
                artifact.run_date,
                artifact.run_key,
                artifact.manifest,
            )
            if staged_digest != digest:
                raise RuntimeError("Publication queue SHA-256 differs from source artifact")
        else:
            queued = existing

        record = self.store.get_run(artifact.run_key)
        if record is not None and record.artifact_sha256 and record.artifact_sha256 != digest:
            raise RuntimeError(f"Publication-store artifact collision for {artifact.run_key}")

        if record is None:
            self.store.begin_named_run(
                artifact.run_key,
                artifact.scope_key,
                artifact.run_date,
            )
            self.store.mark_rendered(
                artifact.run_key,
                artifact.content_format,
                artifact.selection_signature,
                str(queued),
                digest,
            )
        elif record.status in {"running", "failed", "rendered"}:
            self.store.mark_rendered(
                artifact.run_key,
                artifact.content_format,
                artifact.selection_signature,
                str(queued),
                digest,
            )
        else:
            self.store.update_artifact_path(artifact.run_key, str(queued))

        current_state = queued.parent.parent.name
        if target_state == "needs_review" and current_state == "ready":
            queued = self.queue.move_state(queued, artifact.run_key, "needs_review")
            self.store.update_artifact_path(artifact.run_key, str(queued))
            current_state = "needs_review"

        return {
            "run_key": artifact.run_key,
            "scope_key": artifact.scope_key,
            "run_date": artifact.run_date.isoformat(),
            "queue_state": current_state,
            "artifact": str(queued),
            "sha256": digest,
        }

    def publish(
        self,
        run_key: str,
        platforms: Iterable[str],
        *,
        approved: bool,
    ) -> dict:
        """Publish one reviewed queue item with crash-safe per-platform idempotence."""

        if not approved:
            raise RuntimeError("Human approval is required before publication")
        selected = list(dict.fromkeys(platforms))
        if not selected:
            raise ValueError("At least one publication platform is required")
        invalid = [platform for platform in selected if platform not in self.supported_platforms]
        if invalid:
            raise ValueError(f"Unsupported publication platforms: {', '.join(invalid)}")

        artifact = self.queue.locate(run_key)
        if artifact is None:
            raise RuntimeError(f"No staged artifact found for {run_key}")
        manifest = self._load_manifest(artifact)
        title = str(manifest.get("title") or "Generated video")
        description = str(manifest.get("description") or "")

        errors: dict[str, str] = {}
        with FileLock(self.lock_path):
            self.store.recover_interrupted_publications(run_key)
            for platform in selected:
                if not self.store.begin_platform(run_key, platform):
                    continue
                try:
                    publisher = self.publisher_factory(platform)
                    result = publisher.publish(artifact, title, description)
                except PublishError as exc:
                    if exc.outcome_uncertain:
                        self.store.finish_platform(
                            run_key,
                            platform,
                            "unknown",
                            external_id=exc.external_id,
                            error=str(exc),
                        )
                    else:
                        self.store.record_platform_failure(run_key, platform, str(exc))
                    errors[platform] = str(exc)
                except Exception as exc:
                    self.store.record_platform_failure(run_key, platform, str(exc))
                    errors[platform] = str(exc)
                else:
                    self.store.finish_platform(
                        run_key,
                        platform,
                        result.status,
                        external_id=result.external_id,
                    )

            run_status = self.store.finalize_run(run_key)
            platform_statuses = self.store.platform_statuses(run_key)
            queue_state = self._queue_state(run_status, platform_statuses)
            current_state = artifact.parent.parent.name
            if current_state != queue_state:
                artifact = self.queue.move_state(artifact, run_key, queue_state)
                self.store.update_artifact_path(run_key, str(artifact))

        return {
            "run_key": run_key,
            "run_status": run_status,
            "queue_state": queue_state,
            "platforms": platform_statuses,
            "errors": errors,
            "artifact": str(artifact),
        }

    @staticmethod
    def _load_manifest(artifact: Path) -> dict:
        path = artifact.parent / "manifest.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Unable to read publication manifest: {path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Publication manifest must be a JSON object: {path}")
        return payload

    @staticmethod
    def _queue_state(run_status: str, platform_statuses: dict[str, str]) -> str:
        if run_status == "completed":
            if any(
                status in {"submitted", "draft_uploaded"}
                for status in platform_statuses.values()
            ):
                return "submitted"
            return "published"
        if run_status == "partial":
            return "failed"
        return "needs_review"
