"""Builds Agent Protocol v1 telemetry payloads. Only mergeable values are
sent: counts and histograms, never percentiles or rates."""

from __future__ import annotations

import platform
from typing import Optional

from deployangel.core.aggregator import PERIOD_SECONDS, JobStats, Period
from deployangel.core.instance import iso8601
from deployangel.version import VERSION as AGENT_VERSION

VERSION = 1
AGENT_NAME = "deployangel-python"


def telemetry(period: Period, instance, release, runtime: dict, capabilities: list) -> dict:
    return {
        "protocol_version": VERSION,
        "agent": {"name": AGENT_NAME, "version": AGENT_VERSION},
        "runtime": runtime,
        "instance": instance.to_protocol(),
        "release": release.to_protocol(),
        "capabilities": list(capabilities),
        "period": {"started_at": iso8601(period.started_at), "duration_seconds": PERIOD_SECONDS},
        "http": {
            "requests": period.requests,
            "status_counts": dict(period.status_counts),
            "unhandled_exceptions": period.unhandled_exceptions,
            "latency_histogram": period.histogram.to_protocol(),
        },
        "routes": [
            {
                "key": key,
                "requests": route.requests,
                "status_counts": dict(route.status_counts),
                "latency_histogram": route.histogram.to_protocol(),
            }
            for key, route in period.routes.items()
        ],
        "exceptions": [
            {key: value for key, value in dict(entry, sources=dict(entry["sources"])).items() if value is not None}
            for entry in period.exceptions.values()
        ],
        "exceptions_truncated": period.exceptions_truncated,
        "jobs": job_stats(period.jobs),
        "job_classes": [dict({"key": key}, **job_stats(stats)) for key, stats in period.job_classes.items()],
        "checkpoints": [{"key": key, "count": count} for key, count in period.checkpoints.items()],
    }


def job_stats(stats: JobStats) -> dict:
    return {
        "processed": stats.processed,
        "failed": stats.failed,
        "discarded": stats.discarded,
        "duration_histogram": stats.duration.to_protocol(),
        "queue_latency_histogram": stats.queue_latency.to_protocol(),
    }


def runtime(framework: Optional[str] = None, framework_version: Optional[str] = None) -> dict:
    data = {
        "language": "python",
        "language_version": platform.python_version(),
        "framework": framework,
        "framework_version": framework_version,
    }
    return {key: value for key, value in data.items() if value is not None}
