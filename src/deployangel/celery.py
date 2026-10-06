"""Celery integration: every task attempt through Celery's signals, and the
recurring tasks Celery Beat declares.

In Django the app config installs it. Elsewhere, call init() where the Celery
app is created, so worker processes start the agent:

    app = Celery("shop")
    deployangel.celery.init(app)
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import deployangel
from deployangel.core import work
from deployangel.metadata import module_file

# A header added to each task message at publish, read back when it runs, so
# queue latency can be measured. Celery doesn't record when a task was sent.
PUBLISHED_AT = "deployangel_published_at"
FAILED_STATES = ("FAILURE", "RETRY")
PERIODS = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1, "microseconds": 0.000001}

_installed = False
_app = None
# task id -> a stack of (started, queue latency, unit-of-work token), one per
# attempt running now: an eager retry can run inside the attempt it retries,
# under the same id.
_running: dict = {}
_lock = threading.Lock()


def init(app=None, environment: Optional[str] = None):
    """Instruments Celery and starts the agent if nothing else has, for a
    worker that doesn't load a web framework."""
    install(app)
    import celery

    return deployangel.start(environment=environment, framework="celery", framework_version=celery.__version__)


def install(app=None) -> None:
    global _installed, _app
    if app is not None:
        _app = app
    with _lock:
        if _installed:
            return
        _installed = True
    from celery import signals

    signals.before_task_publish.connect(_before_publish, weak=False, dispatch_uid="deployangel.before_publish")
    signals.task_prerun.connect(_prerun, weak=False, dispatch_uid="deployangel.prerun")
    signals.task_postrun.connect(_postrun, weak=False, dispatch_uid="deployangel.postrun")
    signals.task_failure.connect(_failure, weak=False, dispatch_uid="deployangel.failure")
    signals.task_retry.connect(_retry, weak=False, dispatch_uid="deployangel.retry")
    # Prefork pool processes end with os._exit, which skips atexit.
    signals.worker_process_shutdown.connect(_process_shutdown, weak=False, dispatch_uid="deployangel.process_shutdown")
    deployangel.add_capability("jobs")
    deployangel.add_metadata_source(CelerySource())


def _before_publish(sender=None, headers=None, **kwargs) -> None:
    try:
        if deployangel.recording() and isinstance(headers, dict):
            headers.setdefault(PUBLISHED_AT, time.time())
    except Exception:
        pass


def _prerun(sender=None, task_id=None, task=None, **kwargs) -> None:
    try:
        if deployangel.recording() and task_id is not None:
            started = (time.perf_counter(), _queue_latency_ms(task))
            _running.setdefault(task_id, []).append(started + (work.begin(work.JOB),))
    except Exception:
        pass


def _postrun(sender=None, task_id=None, task=None, state=None, **kwargs) -> None:
    try:
        attempts = _running.get(task_id)
        if not attempts:
            return
        perf_started, latency, unit = attempts.pop()
        if not attempts:
            _running.pop(task_id, None)
        # Celery sends task_postrun from a finally block in the thread that ran
        # the task, so the unit of work ends even when the task raised.
        work.end(unit)
        if not deployangel.recording():
            return
        deployangel.record_job(
            job_class=_task_name(task, sender),
            duration_ms=(time.perf_counter() - perf_started) * 1000.0,
            failed=state in FAILED_STATES,
            discarded=state == "FAILURE",
            queue_latency_ms=latency,
        )
    except Exception:
        pass


def _failure(sender=None, exception=None, **kwargs) -> None:
    if isinstance(exception, BaseException):
        deployangel.record_exception(exception, source=f"job_class:{_task_name(sender, None)}")


def _retry(sender=None, reason=None, **kwargs) -> None:
    # reason is Celery's Retry, which carries the exception that caused it
    # (none for a task that retries on purpose, such as one polling), or the
    # exception itself.
    exception = getattr(reason, "exc", None) if type(reason).__name__ == "Retry" else reason
    if isinstance(exception, BaseException):
        deployangel.record_exception(exception, source=f"job_class:{_task_name(sender, None)}")


def _process_shutdown(**kwargs) -> None:
    deployangel.shutdown()


def _task_name(task, sender) -> str:
    return str(getattr(task, "name", None) or getattr(sender, "name", None) or "unknown")


def _queue_latency_ms(task) -> Optional[float]:
    """From when the task became runnable: its ETA (or countdown), else when
    it was published."""
    request = getattr(task, "request", None)
    if request is None:
        return None
    published = _get(request, PUBLISHED_AT)
    runnable_at = float(published) if isinstance(published, (int, float)) else None
    eta = _get(request, "eta")
    if eta:
        try:
            eta_at = (eta if isinstance(eta, datetime) else datetime.fromisoformat(str(eta))).timestamp()
            runnable_at = max(runnable_at or eta_at, eta_at)
        except (TypeError, ValueError):
            pass
    if runnable_at is None:
        return None
    return max((time.time() - runnable_at) * 1000.0, 0.0)


def _get(request, name):
    value = getattr(request, name, None)
    if value is None and isinstance(getattr(request, "headers", None), dict):
        value = request.headers.get(name)
    return value


def current_app():
    if _app is not None:
        return _app
    from celery import current_app as app

    return app._get_current_object() if hasattr(app, "_get_current_object") else app


class CelerySource:
    """Registered tasks and Beat's declared schedule, plus django-celery-beat's
    periodic tasks when that app is installed."""

    def job_classes(self) -> list:
        return [name for name in current_app().tasks.keys() if not name.startswith("celery.")]

    def job_class_files(self) -> dict:
        agent = deployangel.agent()
        root = agent.root if agent else None
        files = {}
        for name, task in list(current_app().tasks.items()):
            if name.startswith("celery."):
                continue
            path = module_file(getattr(task, "__module__", None), root)
            if path:
                files[name] = [path]
        return files

    def schedules(self) -> list:
        app = current_app()
        zone = time_zone_name(app)
        schedules = {}
        for key, entry in dict(app.conf.beat_schedule or {}).items():
            if isinstance(entry, dict) and entry.get("task"):
                schedule = convert(entry.get("schedule"))
                if schedule:
                    schedules[str(key)] = dict({"key": str(key), "class": str(entry["task"]), "source": "celery_beat",
                                                "time_zone": zone}, **schedule)
        # Its database holds the settings' schedule too, under the same names.
        for schedule in django_celery_beat_schedules(zone):
            schedules[schedule["key"]] = schedule
        return list(schedules.values())


def convert(schedule) -> Optional[dict]:
    """{"schedule": cron} for a crontab, {"schedule": None, "every": "300s"}
    for an interval (which repeats from when Beat starts, and a deploy restarts
    it), or None for a solar or custom schedule."""
    from celery.schedules import crontab
    from celery.schedules import schedule as interval

    if isinstance(schedule, crontab):
        fields = [schedule._orig_minute, schedule._orig_hour, schedule._orig_day_of_month,
                  schedule._orig_month_of_year, schedule._orig_day_of_week]
        return {"schedule": " ".join(_cron_field(field) for field in fields)}
    if isinstance(schedule, interval) and type(schedule) is interval:
        return _every(schedule.run_every.total_seconds())
    if isinstance(schedule, timedelta):
        return _every(schedule.total_seconds())
    if isinstance(schedule, (int, float)) and not isinstance(schedule, bool):
        return _every(schedule)
    return None


def _every(seconds: float) -> Optional[dict]:
    seconds = int(round(seconds))
    return {"schedule": None, "every": f"{seconds}s"} if seconds > 0 else None


def _cron_field(field) -> str:
    if isinstance(field, (set, frozenset, list, tuple, range)):
        return ",".join(str(value) for value in sorted(field))
    return str(field).replace(" ", "")


def time_zone_name(app) -> Optional[str]:
    """The zone Beat reads crontabs in: Celery's timezone setting (in Django,
    CELERY_TIMEZONE or TIME_ZONE), UTC by default. None for local time."""
    try:
        zone = app.timezone
    except Exception:
        return None
    key = getattr(zone, "key", None) or getattr(zone, "zone", None)
    if key:
        return str(key)
    if zone is timezone.utc or str(zone) in ("UTC", "UTC+00:00"):
        return "UTC"
    return None


def django_celery_beat_schedules(default_zone: Optional[str]) -> list:
    """Enabled, recurring periodic tasks from django-celery-beat's tables.
    Read in the reporter thread, whose database connection is closed after."""
    if "django" not in sys.modules:
        return []
    try:
        from django.apps import apps

        if not apps.ready or not apps.is_installed("django_celery_beat"):
            return []
        from django.db import connections
        from django_celery_beat.models import PeriodicTask
    except Exception:
        return []
    schedules = []
    try:
        tasks = PeriodicTask.objects.filter(enabled=True, one_off=False).select_related("crontab", "interval")
        for task in tasks[:200]:
            if task.expires and task.expires < datetime.now(timezone.utc):
                continue
            entry = {"key": task.name, "class": task.task, "source": "django_celery_beat", "time_zone": default_zone}
            if task.crontab is not None:
                crontab = task.crontab
                entry["schedule"] = " ".join(str(value).replace(" ", "") for value in (
                    crontab.minute, crontab.hour, crontab.day_of_month, crontab.month_of_year, crontab.day_of_week))
                zone = getattr(crontab.timezone, "key", None) or (str(crontab.timezone) if crontab.timezone else None)
                entry["time_zone"] = zone or default_zone
            elif task.interval is not None:
                every = _every(task.interval.every * PERIODS.get(task.interval.period, 0))
                if not every:
                    continue
                entry.update(every)
            else:
                continue
            schedules.append(entry)
    except Exception:
        return schedules
    finally:
        try:
            if threading.current_thread() is not threading.main_thread():
                connections.close_all()
        except Exception:
            pass
    return schedules
