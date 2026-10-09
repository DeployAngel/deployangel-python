"""Work that runs on a schedule outside the app's job system: code that cron,
Heroku Scheduler, a Kubernetes CronJob, or django-crontab starts. task()
records it as a job run under its name, with its duration and whether it
raised, and config.recurring_jobs declares when it should run, so DeployAngel
expects it like any recurring job."""

from __future__ import annotations

import functools
import os
import re
import time
from typing import Optional

from deployangel.core import work

NAME = re.compile(r"\A\S[^\x00-\x1f\x7f]{0,199}\Z")
MANAGE_PREFIX = "manage.py "

_warned = False


class task:
    """Records code as a run of scheduled work:

        with deployangel.task("nightly import"):
            run_import()

    or, on a function:

        @deployangel.task("nightly import")
        def run_import(): ...

    Exceptions are recorded and re-raised untouched. When the agent isn't
    recording, the code just runs."""

    def __init__(self, name: str):
        self.name = str(name)
        self._started: Optional[float] = None
        self._unit = None

    def __enter__(self) -> "task":
        import deployangel

        if deployangel.recording() and _valid(self.name):
            deployangel.add_capability("jobs")
            self._unit = work.begin(work.JOB)
            self._started = time.monotonic()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if self._started is None:
            return False
        import deployangel

        try:
            if exc is not None:
                deployangel.record_exception(exc, source=f"job_class:{self.name}")
            deployangel.record_job(self.name, (time.monotonic() - self._started) * 1000.0, failed=exc is not None)
        except Exception:
            pass
        finally:
            work.end(self._unit)
            self._started = self._unit = None
        return False

    def __call__(self, function):
        name = self.name

        @functools.wraps(function)
        def recorded(*args, **kwargs):
            with task(name):
                return function(*args, **kwargs)

        return recorded


def _valid(name: str) -> bool:
    global _warned
    if NAME.match(name):
        return True
    if not _warned:
        import deployangel

        deployangel.configuration().logger.warning(
            "DeployAngel didn't record task %r: use a name of up to 200 printable characters", name)
        _warned = True
    return False


class ConfiguredSchedules:
    """config.recurring_jobs, by the name the work runs under: a Celery or RQ
    task, "manage.py <command>", or a task() name. A schedule without a zone
    is read in the server's."""

    def __init__(self, config):
        self.config = config

    def schedules(self) -> list:
        zone = local_time_zone()
        return [{"key": str(name), "class": str(name), "schedule": str(schedule), "source": "config", "time_zone": zone}
                for name, schedule in dict(self.config.recurring_jobs or {}).items()
                if str(name).strip() and str(schedule).strip()]


def local_time_zone() -> Optional[str]:
    """The zone cron reads schedules in on this server: TZ, else the zone
    /etc/localtime points to. None when neither says, and DeployAngel then
    doesn't assume one."""
    zone = os.environ.get("TZ", "").lstrip(":")
    if zone and ("/" in zone or zone == "UTC"):
        return zone
    try:
        target = os.path.realpath("/etc/localtime")
        marker = "zoneinfo" + os.sep
        if marker in target:
            return target.split(marker, 1)[1]
    except Exception:
        pass
    return None


def manage_commands(schedules: list) -> set:
    """The management commands a schedule names ("manage.py send_invoices")."""
    return {str(s.get("class"))[len(MANAGE_PREFIX):].strip() for s in schedules
            if str(s.get("class") or "").startswith(MANAGE_PREFIX)}
