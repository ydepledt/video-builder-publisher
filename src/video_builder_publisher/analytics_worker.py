"""Long-lived periodic execution for shared publication analytics collectors."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from typing import Any, TextIO

from .security import redact_sensitive_text

DEFAULT_ANALYTICS_INTERVAL_MINUTES = 60


def run_periodic_analytics(
    collect_once: Callable[[], dict[str, Any]],
    *,
    interval_minutes: int = DEFAULT_ANALYTICS_INTERVAL_MINUTES,
    worker_name: str = "analytics",
    max_runs: int | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    """Run one analytics collector repeatedly without coupling it to an application.

    The application owns credentials, paths and the ``collect_once`` callback. This
    helper owns only interval scheduling, structured logging and secret-safe error
    reporting. ``max_runs`` and the injectable clock/sleep hooks make the loop
    deterministic in tests; production callers normally leave them unset.
    """

    if interval_minutes < 1:
        raise ValueError("interval_minutes must be >= 1")
    if max_runs is not None and max_runs < 1:
        raise ValueError("max_runs must be >= 1 when provided")

    out = stdout or sys.stdout
    err = stderr or sys.stderr
    interval_seconds = interval_minutes * 60.0
    completed = 0

    while max_runs is None or completed < max_runs:
        started = monotonic()
        try:
            result = collect_once()
        except Exception as exc:
            diagnostic = {
                "format": "video.analytics.worker.error.v1",
                "worker": worker_name,
                "error": redact_sensitive_text(exc),
            }
            print(
                json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
                file=err,
                flush=True,
            )
        else:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), file=out, flush=True)

        completed += 1
        if max_runs is not None and completed >= max_runs:
            break

        elapsed = max(0.0, monotonic() - started)
        sleep(max(1.0, interval_seconds - elapsed))
