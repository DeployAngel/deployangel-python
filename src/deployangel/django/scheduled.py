"""Scheduled work in a Django app outside its job system: django-crontab's
CRONJOBS setting, and management commands that cron or Heroku Scheduler runs.
Each is recorded as a job run by name (deployangel.task), so DeployAngel can
expect it on schedule."""

from __future__ import annotations

import importlib
from typing import Optional

import deployangel
from deployangel.scheduled import MANAGE_PREFIX, local_time_zone, manage_commands, task

CALL_COMMAND = "django.core.management.call_command"

_scheduled_commands: set = set()
_command_hook_installed = False


class DjangoCrontab:
    """django-crontab writes each CRONJOBS entry into the server's crontab, so
    its times are read in the server's zone."""

    def schedules(self) -> list:
        zone = local_time_zone()
        schedules = []
        for entry in crontab_jobs():
            name = job_name(entry)
            if name:
                schedules.append({"key": name, "class": name, "schedule": str(entry[0]), "source": "django_crontab",
                                  "time_zone": zone})
        return schedules


def crontab_jobs() -> list:
    try:
        from django.apps import apps
        from django.conf import settings

        if not apps.is_installed("django_crontab"):
            return []
        return [job for job in (getattr(settings, "CRONJOBS", None) or [])
                if isinstance(job, (list, tuple)) and len(job) >= 2 and str(job[0]).strip() and str(job[1]).strip()]
    except Exception:
        return []


def job_name(entry) -> Optional[str]:
    """A function's dotted path, or "manage.py <command>" for an entry that
    calls a management command."""
    path = str(entry[1])
    if path == CALL_COMMAND:
        args = entry[2] if len(entry) > 2 and isinstance(entry[2], (list, tuple)) else []
        return f"{MANAGE_PREFIX}{args[0]}" if args else None
    return path


def install(schedules: list) -> None:
    """Wraps the functions django-crontab runs, and the management commands any
    schedule names, so their runs are recorded with no change to the app."""
    for entry in crontab_jobs():
        path = str(entry[1])
        if path != CALL_COMMAND:
            _wrap_function(path)
    _scheduled_commands.update(manage_commands(schedules))
    if _scheduled_commands:
        _install_command_hook()


def _wrap_function(path: str) -> None:
    """django-crontab imports the module and looks the function up by name when
    the job runs, so replacing it in its module is enough."""
    try:
        module_path, name = path.rsplit(".", 1)
        module = importlib.import_module(module_path)
        function = getattr(module, name)
        if getattr(function, "__deployangel_task__", False):
            return
        wrapped = task(path)(function)
        wrapped.__deployangel_task__ = True
        setattr(module, name, wrapped)
    except Exception as error:
        deployangel.configuration().logger.warning("DeployAngel couldn't watch cron job %s: %s: %s", path,
                                                  type(error).__name__, error)


def _install_command_hook() -> None:
    global _command_hook_installed
    if _command_hook_installed:
        return
    from django.core.management.base import BaseCommand

    original = BaseCommand.execute

    def execute(self, *args, **options):
        name = command_name(self)
        if name not in _scheduled_commands:
            return original(self, *args, **options)
        with task(f"{MANAGE_PREFIX}{name}"):
            return original(self, *args, **options)

    BaseCommand.execute = execute
    _command_hook_installed = True


def command_name(command) -> str:
    """A command is named after its module: myapp/management/commands/send_invoices.py."""
    return type(command).__module__.rsplit(".", 1)[-1]
