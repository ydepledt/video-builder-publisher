from __future__ import annotations

import io
import json

import pytest

from video_builder_publisher import run_periodic_analytics


def test_periodic_worker_logs_success_and_respects_remaining_interval() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    sleeps: list[float] = []
    clock = iter([10.0, 15.0, 20.0, 21.0])
    calls = 0

    def collect_once():
        nonlocal calls
        calls += 1
        return {"format": "video.analytics.collection.v1", "collected": calls}

    run_periodic_analytics(
        collect_once,
        interval_minutes=1,
        max_runs=2,
        stdout=stdout,
        stderr=stderr,
        sleep=sleeps.append,
        monotonic=lambda: next(clock),
    )

    rows = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [row["collected"] for row in rows] == [1, 2]
    assert sleeps == [55.0]
    assert stderr.getvalue() == ""


def test_periodic_worker_redacts_errors_and_continues() -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = 0

    def collect_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("request failed: Bearer super-secret")
        return {"collected": 1}

    run_periodic_analytics(
        collect_once,
        interval_minutes=1,
        worker_name="earthpulse",
        max_runs=2,
        stdout=stdout,
        stderr=stderr,
        sleep=lambda seconds: None,
        monotonic=lambda: 0.0,
    )

    diagnostic = json.loads(stderr.getvalue())
    assert diagnostic["format"] == "video.analytics.worker.error.v1"
    assert diagnostic["worker"] == "earthpulse"
    assert "super-secret" not in diagnostic["error"]
    assert "<redacted>" in diagnostic["error"]
    assert json.loads(stdout.getvalue())["collected"] == 1


@pytest.mark.parametrize("interval", [0, -1])
def test_periodic_worker_rejects_invalid_interval(interval: int) -> None:
    with pytest.raises(ValueError, match="interval_minutes"):
        run_periodic_analytics(lambda: {}, interval_minutes=interval, max_runs=1)


def test_periodic_worker_rejects_invalid_max_runs() -> None:
    with pytest.raises(ValueError, match="max_runs"):
        run_periodic_analytics(lambda: {}, max_runs=0)
