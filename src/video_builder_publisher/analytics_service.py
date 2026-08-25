"""Reusable orchestration for publication analytics collection."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .analytics import (
    AnalyticsProfile,
    AnalyticsStore,
    AnalyticsTarget,
    PublicationAnalyticsSource,
    PublicationAnalyticsTarget,
)
from .analytics_platforms import AnalyticsError


class AnalyticsClient(Protocol):
    platform: str

    def fetch_metrics(
        self,
        external_id: str,
        *,
        published_at: str,
        duration_seconds: float | None,
        observed_at: str,
    ): ...


class AnalyticsCollector:
    """Generator-agnostic sync/collect/link/report workflow.

    Applications provide credentials/client construction plus an ``AnalyticsProfile``.
    The collector owns the common publication-target sync, checkpoint assignment,
    retention collection and failure isolation. ``scope_key=None`` means all scopes,
    which is useful for generators that publish multiple charts/regions from one store.
    """

    def __init__(
        self,
        *,
        store: AnalyticsStore,
        publication_source: PublicationAnalyticsSource,
        profile: AnalyticsProfile,
        scope_key: str | None,
        supported_platforms: Iterable[str],
        client_factory: Callable[[str], AnalyticsClient],
        is_configured: Callable[[str], bool],
        requires_manual_link: Callable[[PublicationAnalyticsTarget], bool] | None = None,
        retention_platforms: Iterable[str] = ("youtube",),
        retention_checkpoints: Iterable[int] = (24, 168),
    ) -> None:
        self.store = store
        self.publication_source = publication_source
        self.profile = profile
        self.scope_key = scope_key
        self.supported_platforms = tuple(dict.fromkeys(supported_platforms))
        self.client_factory = client_factory
        self.is_configured = is_configured
        self.requires_manual_link = requires_manual_link
        self.retention_platforms = frozenset(retention_platforms)
        self.retention_checkpoints = frozenset(int(value) for value in retention_checkpoints)

    def sync(self, *, run_key: str | None = None) -> dict[str, int]:
        return self.store.sync_publications(
            self.publication_source,
            self.profile,
            scope_key=self.scope_key,
            run_key=run_key,
            requires_manual_link=self.requires_manual_link,
        )

    def link_target(
        self,
        run_key: str,
        platform: str,
        external_id: str,
        *,
        published_at: str | None = None,
    ) -> dict[str, str]:
        self._validate_platforms([platform])
        self.sync(run_key=run_key)
        inferred = published_at
        resolved_scope = self.scope_key
        existing = self.store.get_target(run_key, platform)
        if existing is not None:
            resolved_scope = existing.scope_key
            if inferred is None:
                inferred = existing.published_at

        if inferred is None or resolved_scope is None:
            rows = self.publication_source.targets(
                scope_key=self.scope_key,
                run_key=run_key,
                platforms=[platform],
            )
            if rows:
                resolved_scope = rows[0].scope_key
                if inferred is None:
                    inferred = rows[0].published_at

        if inferred is None:
            raise RuntimeError(
                "Publication timestamp is unknown. Supply a timezone-aware published_at."
            )
        if resolved_scope is None:
            raise RuntimeError("Publication scope is unknown for manual analytics linking.")

        normalized = _aware_timestamp(inferred)
        self.store.link_target(
            run_key,
            resolved_scope,
            platform,
            external_id,
            normalized,
            source="manual",
            force=True,
        )
        return {
            "run_key": run_key,
            "platform": platform,
            "external_id": external_id,
            "published_at": normalized,
            "source": "manual",
        }

    def collect(
        self,
        *,
        run_key: str | None = None,
        platforms: Iterable[str] | None = None,
        max_age_days: int = 14,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if max_age_days < 1:
            raise ValueError("max_age_days must be >= 1")
        selected = tuple(dict.fromkeys(platforms or self.supported_platforms))
        self._validate_platforms(selected)
        sync_result = self.sync(run_key=run_key)

        observation = _aware_timestamp(observed_at or _now())
        observation_dt = _parse_aware(observation)
        cutoff = observation_dt - timedelta(days=max_age_days)
        targets = [
            target
            for target in self.store.targets(
                run_key=run_key,
                scope_key=self.scope_key,
                platforms=selected,
            )
            if cutoff <= _parse_aware(target.published_at) <= observation_dt
        ]

        collection_id = self.store.start_collection_run()
        collected = 0
        no_data = 0
        skipped = 0
        errors: dict[str, str] = {}
        clients: dict[str, AnalyticsClient] = {}

        for target in targets:
            key = f"{target.run_key}|{target.platform}"
            if not self.is_configured(target.platform):
                skipped += 1
                errors[key] = f"{target.platform} analytics credentials are not configured"
                continue
            try:
                client = clients.get(target.platform)
                if client is None:
                    client = self.client_factory(target.platform)
                    clients[target.platform] = client
                snapshot = client.fetch_metrics(
                    target.external_id,
                    published_at=target.published_at,
                    duration_seconds=target.duration_seconds,
                    observed_at=observation,
                )
                _, assigned = self.store.insert_snapshot(target, observation, snapshot)
                if snapshot.status == "ok":
                    collected += 1
                else:
                    no_data += 1
                if snapshot.status == "ok":
                    self._collect_retention(client, target, assigned, observation, errors)
            except AnalyticsError as exc:
                errors[key] = str(exc)
            except Exception as exc:
                errors[key] = f"Unexpected analytics error: {type(exc).__name__}"

        self.store.finish_collection_run(
            collection_id,
            eligible_targets=len(targets),
            collected=collected,
            no_data=no_data,
            skipped=skipped,
            errors=errors,
        )
        return {
            "format": "video.analytics.collection.v1",
            "scope_key": self.scope_key,
            "observed_at": observation,
            "collection_id": collection_id,
            "eligible_targets": len(targets),
            "collected": collected,
            "no_data": no_data,
            "skipped": skipped,
            "errors": errors,
            "sync": sync_result,
        }

    def report(self, mode: str = "checkpoints") -> list[dict[str, Any]]:
        if mode == "checkpoints":
            return self.store.checkpoint_report()
        if mode == "latest":
            return self.store.latest_report()
        if mode == "retention":
            return self.store.retention_report()
        raise ValueError(f"Unsupported analytics report mode: {mode}")

    def _collect_retention(
        self,
        client: AnalyticsClient,
        target: AnalyticsTarget,
        assigned: list[int],
        observed_at: str,
        errors: dict[str, str],
    ) -> None:
        if target.platform not in self.retention_platforms:
            return
        fetch_retention = getattr(client, "fetch_retention", None)
        if not callable(fetch_retention):
            return
        for checkpoint in assigned:
            if checkpoint not in self.retention_checkpoints:
                continue
            try:
                points = fetch_retention(
                    target.external_id,
                    published_at=target.published_at,
                    observed_at=observed_at,
                )
                if points:
                    self.store.replace_retention(
                        target.run_key,
                        target.platform,
                        checkpoint,
                        points,
                    )
            except AnalyticsError as exc:
                errors[f"{target.run_key}|{target.platform}|retention|{checkpoint}h"] = str(exc)

    def _validate_platforms(self, platforms: Iterable[str]) -> None:
        invalid = [value for value in platforms if value not in self.supported_platforms]
        if invalid:
            raise ValueError(f"Unsupported analytics platforms: {', '.join(invalid)}")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _parse_aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must be timezone-aware: {value}")
    return parsed.astimezone(UTC)


def _aware_timestamp(value: str) -> str:
    return _parse_aware(value).isoformat(timespec="seconds")
