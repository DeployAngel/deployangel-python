"""RQ integration: worker classes that record every job attempt.

    rq worker -w deployangel.rq.Worker            # or deployangel.rq.SimpleWorker
    python manage.py rqworker --worker-class deployangel.rq.Worker   # django-rq

RQ's default worker runs each job in a forked work horse that exits as soon as
the job ends, too soon to report anything itself. So the worker process
records each job once its horse is done, from what RQ saved in Redis: the
outcome, timings, and for a failed job its formatted traceback."""

from __future__ import annotations

import time
from typing import Optional

import rq
from rq.job import JobStatus

import deployangel
from deployangel.core import fingerprint

RETRYING = (JobStatus.SCHEDULED, JobStatus.QUEUED, JobStatus.DEFERRED)
ENDED_WITHOUT_FAILING = (JobStatus.STOPPED, JobStatus.CANCELED)

deployangel.add_capability("jobs")


class InstrumentedWorkerMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            deployangel.start(framework="rq", framework_version=getattr(rq, "__version__", None), eager=True)
        except Exception:
            pass

    def execute_job(self, job, queue):
        if not deployangel.recording():
            return super().execute_job(job, queue)
        started = time.perf_counter()
        retries_left = getattr(job, "retries_left", None)
        try:
            return super().execute_job(job, queue)
        finally:
            record(job, started, retries_left)


class Worker(InstrumentedWorkerMixin, rq.Worker):
    pass


class SimpleWorker(InstrumentedWorkerMixin, rq.SimpleWorker):
    pass


def record(job, started: float, retries_left: Optional[int]) -> None:
    try:
        name = str(job.func_name or "unknown")
        duration_ms = (time.perf_counter() - started) * 1000.0
        try:
            job.refresh()
        except Exception:
            # A job with result_ttl=0 is deleted as soon as it succeeds.
            deployangel.record_job(job_class=name, duration_ms=duration_ms)
            return
        status = job.get_status(refresh=False)
        if job.started_at and job.ended_at:
            duration_ms = max((job.ended_at - job.started_at).total_seconds() * 1000.0, 0.0)
        failed = discarded = False
        if status == JobStatus.FAILED:
            failed = discarded = True
            _record_exception(job, name)
        elif status in RETRYING and retries_left is not None and (job.retries_left or 0) < retries_left:
            failed = True
        elif status in ENDED_WITHOUT_FAILING:
            discarded = True
        deployangel.record_job(job_class=name, duration_ms=duration_ms, failed=failed, discarded=discarded,
                               queue_latency_ms=_queue_latency_ms(job))
    except Exception:
        pass


def _queue_latency_ms(job) -> Optional[float]:
    try:
        if job.enqueued_at and job.started_at:
            return max((job.started_at - job.enqueued_at).total_seconds() * 1000.0, 0.0)
    except Exception:
        return None
    return None


def _record_exception(job, name: str) -> None:
    agent = deployangel.agent()
    if agent is None:
        return
    result = job.latest_result() if hasattr(job, "latest_result") else None
    text = getattr(result, "exc_string", None) or getattr(job, "exc_info", None)
    parsed = fingerprint.parse_traceback(str(text or ""))
    if parsed is None:
        return
    class_name, frames, message = parsed
    messages = agent.config.exception_messages
    details = fingerprint.details(class_name, frames, agent.root, message=message if messages else None)
    agent.record_exception_details(details, fingerprint.app_backtrace(frames, agent.root), source=f"job_class:{name}")
