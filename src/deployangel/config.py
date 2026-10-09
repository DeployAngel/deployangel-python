"""Agent settings: environment variables by default, overridable in code with
deployangel.configure() or, in Django, a DEPLOYANGEL dict in settings."""

from __future__ import annotations

import logging
import os
from typing import Mapping, Optional

TRUE_VALUES = ("1", "true", "yes", "on")
DEFAULT_ENDPOINT = "https://api.deployangel.com"


def parse_boolean(value: Optional[str]) -> Optional[bool]:
    if value is None or not value.strip():
        return None
    return value.strip().lower() in TRUE_VALUES


class Configuration:
    def __init__(self, env: Mapping[str, str] = os.environ):
        self.token = env.get("DEPLOYANGEL_TOKEN")
        self.endpoint = env.get("DEPLOYANGEL_URL") or DEFAULT_ENDPOINT
        self.enabled = parse_boolean(env.get("DEPLOYANGEL_ENABLED"))
        self.environments = ["production"]
        # The deployment's name, such as "production". Python frameworks
        # don't name one, so it comes from here, else the framework's debug
        # flag (debug is "development"), else "production".
        self.environment = env.get("DEPLOYANGEL_ENVIRONMENT") or None
        self.release_version = env.get("DEPLOYANGEL_RELEASE_VERSION")
        self.revision = env.get("DEPLOYANGEL_REVISION")
        # The app's root, which repository paths are relative to. The working
        # directory unless set; on Heroku and in most containers they're the
        # same.
        self.root = env.get("DEPLOYANGEL_ROOT") or None
        self.flush_interval = 60
        self.timeout = 5.0
        self.max_queued_payloads = 10
        self.max_routes = 100
        self.logger = logging.getLogger("deployangel")
        self.file_digests = parse_boolean(env.get("DEPLOYANGEL_FILE_DIGESTS")) is not False
        # Off, exceptions are sent with their class and frames only, never a
        # message, for apps whose messages may hold personal or health data.
        self.exception_messages = parse_boolean(env.get("DEPLOYANGEL_EXCEPTION_MESSAGES")) is not False
        self.critical_flows: dict = {}
        # Route keys as the dashboard shows them, such as "GET /healthz/".
        self.ignored_routes: list = []
        # Work scheduled outside the app's own scheduler (cron, Heroku
        # Scheduler, a Kubernetes CronJob), by the name it runs under: a task,
        # "manage.py <command>", or a deployangel.task() name. Each schedule is
        # a cron line or words like "every day at 4am", optionally ending in a
        # time zone.
        self.recurring_jobs: dict = {}

    def update(self, **options) -> None:
        for name, value in options.items():
            if not hasattr(self, name):
                raise TypeError(f"unknown DeployAngel setting {name!r}")
            setattr(self, name, value)

    # Reports only with a token. By default only in the listed
    # environments; DEPLOYANGEL_ENABLED forces it on or off.
    def is_active(self, environment: str) -> bool:
        if not self.token or not self.endpoint:
            return False
        if self.enabled is not None:
            return self.enabled
        return str(environment) in self.environments

    # Whether requests to a route are left out. HEAD is answered by the GET
    # view, so ignoring "GET /healthz/" ignores "HEAD /healthz/" too.
    def is_ignored_route(self, key: str) -> bool:
        if not self.ignored_routes:
            return False
        if key in self.ignored_routes:
            return True
        return key.startswith("HEAD ") and "GET " + key[5:] in self.ignored_routes
