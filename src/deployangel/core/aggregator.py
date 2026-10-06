"""Accumulates request, job, exception, and checkpoint behavior into
one-minute periods inside the process. Recording is a few dict updates under
a lock; nothing here touches the network."""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from deployangel.core import work
from deployangel.core.histogram import Histogram

OTHER = "__other__"
PERIOD_SECONDS = 60
MAX_EXCEPTIONS = 20
MAX_BACKTRACES = 5
MAX_CHECKPOINTS = 100


class RouteStats:
    __slots__ = ("requests", "status_counts", "histogram")

    def __init__(self) -> None:
        self.requests = 0
        self.status_counts: dict[str, int] = {}
        self.histogram = Histogram()


class JobStats:
    __slots__ = ("processed", "failed", "discarded", "duration", "queue_latency")

    def __init__(self) -> None:
        self.processed = 0
        self.failed = 0
        self.discarded = 0
        self.duration = Histogram()
        self.queue_latency = Histogram()

    def record(self, failed: bool, discarded: bool, duration_ms: float, queue_latency_ms: Optional[float]) -> None:
        self.processed += 1
        if failed:
            self.failed += 1
        if discarded:
            self.discarded += 1
        self.duration.record(duration_ms)
        if queue_latency_ms is not None:
            self.queue_latency.record(queue_latency_ms)

    def record_discard(self) -> None:
        self.discarded += 1


def _bounded_key(table: dict, key: str, limit: int) -> str:
    """The key itself while the table has room, else OTHER: up to limit - 1
    distinct keys per period, the rest counted together."""
    return key if key in table or len(table) < limit - 1 else OTHER


class Period:
    def __init__(self, started_at: int):
        self.started_at = started_at
        self.requests = 0
        self.status_counts: dict[str, int] = {}
        self.unhandled_exceptions = 0
        self.histogram = Histogram()
        self.routes: dict[str, RouteStats] = {}
        self.jobs = JobStats()
        self.job_classes: dict[str, JobStats] = {}
        self.exceptions: dict[str, dict] = {}
        self.exceptions_truncated = 0
        # name -> [count, recorded in an HTTP request, recorded in a job]
        self.checkpoints: dict[str, list] = {}

    def record(self, route_key: str, status: int, duration_ms: float, unhandled: bool, max_routes: int, in_totals: bool) -> None:
        if in_totals:
            self.requests += 1
            if status >= 400:
                self.status_counts[str(status)] = self.status_counts.get(str(status), 0) + 1
            if unhandled:
                self.unhandled_exceptions += 1
            self.histogram.record(duration_ms)

        key = _bounded_key(self.routes, route_key, max_routes)
        route = self.routes.get(key)
        if route is None:
            route = self.routes[key] = RouteStats()
        route.requests += 1
        if status >= 400:
            route.status_counts[str(status)] = route.status_counts.get(str(status), 0) + 1
        route.histogram.record(duration_ms)

    def job_stats(self, job_class: str, max_classes: int) -> JobStats:
        key = _bounded_key(self.job_classes, job_class, max_classes)
        stats = self.job_classes.get(key)
        if stats is None:
            stats = self.job_classes[key] = JobStats()
        return stats

    def record_checkpoint(self, name: str, count: int, unit_of_work: Optional[str] = None) -> None:
        key = _bounded_key(self.checkpoints, name, MAX_CHECKPOINTS)
        counts = self.checkpoints.get(key)
        if counts is None:
            counts = self.checkpoints[key] = [0, 0, 0]
        counts[0] += count
        if unit_of_work == work.HTTP:
            counts[1] += count
        elif unit_of_work == work.JOB:
            counts[2] += count

    def record_exception(self, details: dict, source: Optional[str], handled: bool, backtrace: Optional[list]) -> None:
        """Up to 20 fingerprints per period; the rest are only counted."""
        entry = self.exceptions.get(details["fingerprint"])
        if entry is None:
            if len(self.exceptions) >= MAX_EXCEPTIONS:
                self.exceptions_truncated += 1
                return
            entry = self.exceptions[details["fingerprint"]] = dict(details, count=0, handled_count=0, sources={})
        if handled:
            entry["handled_count"] += 1
        else:
            entry["count"] += 1
        if source:
            entry["sources"][source] = entry["sources"].get(source, 0) + 1
        if backtrace and not entry.get("backtrace"):
            entry["backtrace"] = backtrace


class Aggregator:
    def __init__(self, max_routes: int = 100, clock: Callable[[], float] = time.time):
        self.max_routes = max_routes
        self._clock = clock
        self._periods: dict[int, Period] = {}
        self._seen_fingerprints: set = set()
        self._last_drained_at = self._period_start(clock()) - PERIOD_SECONDS
        self._lock = threading.Lock()

    def _period(self) -> Period:
        started_at = self._period_start(self._clock())
        period = self._periods.get(started_at)
        if period is None:
            period = self._periods[started_at] = Period(started_at)
        return period

    def record(self, route_key: str, status: int, duration_ms: float, unhandled: bool = False, in_totals: bool = True) -> None:
        """in_totals=False records the request under its route only, leaving it
        out of the app-wide counts and latency."""
        with self._lock:
            self._period().record(route_key, int(status), duration_ms, unhandled, self.max_routes, in_totals)

    def record_job(self, job_class: str, duration_ms: float, failed: bool = False, discarded: bool = False,
                   queue_latency_ms: Optional[float] = None) -> None:
        """failed: the attempt raised (or was retried); discarded: the job will
        not run again."""
        with self._lock:
            period = self._period()
            period.jobs.record(failed, discarded, duration_ms, queue_latency_ms)
            period.job_stats(str(job_class), self.max_routes).record(failed, discarded, duration_ms, queue_latency_ms)

    def record_discard(self, job_class: str) -> None:
        """A job that will not run again, recorded without another attempt."""
        with self._lock:
            period = self._period()
            period.jobs.record_discard()
            period.job_stats(str(job_class), self.max_routes).record_discard()

    def record_checkpoint(self, name: str, count: int = 1, unit_of_work: Optional[str] = None) -> None:
        """unit_of_work: work.HTTP or work.JOB when the checkpoint was recorded
        while handling a request or running a job, else None."""
        with self._lock:
            self._period().record_checkpoint(name, count, unit_of_work)

    def record_exception(self, details: dict, source: Optional[str] = None, handled: bool = False,
                         backtrace: Optional[list] = None) -> None:
        """A representative backtrace is kept only the first time this process
        sees a fingerprint, and at most 5 per period."""
        with self._lock:
            period = self._period()
            new_here = details["fingerprint"] not in self._seen_fingerprints
            self._seen_fingerprints.add(details["fingerprint"])
            keep_trace = new_here and sum(1 for e in period.exceptions.values() if e.get("backtrace")) < MAX_BACKTRACES
            period.record_exception(details, source, handled, backtrace if keep_trace else None)

    def drain(self, include_current: bool = False, max_periods: int = 10) -> list:
        """Completed periods, oldest first. Minutes with nothing recorded are
        returned as empty periods, so idle processes still report their
        release. include_current also closes the in-progress minute (used at
        shutdown)."""
        current = self._period_start(self._clock())
        last = current if include_current else current - PERIOD_SECONDS
        with self._lock:
            first = max(self._last_drained_at + PERIOD_SECONDS, last - (max_periods - 1) * PERIOD_SECONDS)
            drained = [self._periods.pop(started_at, None) or Period(started_at)
                       for started_at in range(first, last + 1, PERIOD_SECONDS)]
            for started_at in [s for s in self._periods if s <= last]:
                del self._periods[started_at]
            if drained:
                self._last_drained_at = last
            return drained

    @staticmethod
    def _period_start(now: float) -> int:
        seconds = int(now)
        return seconds - seconds % PERIOD_SECONDS
