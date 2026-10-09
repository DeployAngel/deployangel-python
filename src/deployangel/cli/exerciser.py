"""`deployangel exercise`: sends a release's exercise plan's read-only
requests to production from the customer's side, then records what it sent,
so the release page says which routes were exercised that way. Only GET
routes with no path parameters and not marked as changing data; the rest
are skipped and named, for the agent or a smoke test with a test account.
The requests count like any traffic through the app's agent. Behaves like
the Ruby gem's Exerciser."""

from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

from deployangel.version import VERSION

MAX_REQUESTS = 200
INTERVAL = 0.2  # about 5 requests a second
TIMEOUT = 10
# Stop when the app isn't answering, rather than send the rest into it.
MAX_CONSECUTIVE_ERRORS = 5
# Rails' :id and *path, Django's <int:pk>, FastAPI's {id}.
PARAMETER = re.compile(r"/[:*{<]")


@dataclass
class Target:
    key: str
    path: str
    count: int


@dataclass
class Result:
    routes: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    sent: int = 0
    unreachable: bool = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects aren't followed: the request already reached the app."""

    def redirect_request(self, *args, **kwargs):
        return None


def _get(url: str) -> Optional[int]:
    """The status code, or None when there was no answer."""
    request = urllib.request.Request(url, method="GET")
    request.add_header("User-Agent", f"DeployAngel-Exercise/{VERSION} (+https://www.deployangel.com/docs#exercise)")
    request.add_header("Accept", "text/html,application/json;q=0.9,*/*;q=0.8")
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=TIMEOUT) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError, ValueError):
        return None


class Exerciser:
    def __init__(self, base_url: str, max_requests: int = MAX_REQUESTS, sleeper: Callable[[float], None] = None,
                 requester: Optional[Callable[[str], Optional[int]]] = None):
        import time

        self.base = urllib.parse.urlsplit(base_url)
        self.max_requests = max_requests
        self.sleeper = sleeper or time.sleep
        self.requester = requester or _get

    def plan(self, exercise_plan: dict):
        """What would be sent: (targets, skipped)."""
        targets, skipped = [], []
        for item in exercise_plan.get("items") or []:
            if item.get("kind") != "route":
                continue
            method, _, path = str(item.get("key", "")).partition(" ")
            reason = None
            if item.get("mutating") or method != "GET":
                reason = "changes data"
            elif not path or PARAMETER.search(path):
                reason = "needs a path parameter"
            if reason:
                skipped.append({"key": item.get("key"), "reason": reason})
                continue
            needed = int(item["runs_needed"]) - int(item.get("runs") or 0) if item.get("runs_needed") else 1
            targets.append(Target(item["key"], path, max(needed, 1)))
        self._spread_shortfall(targets, (exercise_plan.get("shortfall") or {}).get("requests"))
        while sum(target.count for target in targets) > self.max_requests:
            max(targets, key=lambda target: target.count).count -= 1
        return targets, skipped

    def run(self, exercise_plan: dict) -> Result:
        targets, skipped = self.plan(exercise_plan)
        result = Result(skipped=skipped)
        errors_in_a_row = 0
        for target in targets:
            statuses = {}
            for _ in range(target.count):
                if errors_in_a_row >= MAX_CONSECUTIVE_ERRORS:
                    break
                if result.sent:
                    self.sleeper(INTERVAL)
                status = self.requester(self.url_for(target.path))
                result.sent += 1
                kind = f"{status // 100}xx" if status else "error"
                statuses[kind] = statuses.get(kind, 0) + 1
                errors_in_a_row = errors_in_a_row + 1 if kind == "error" else 0
            requests = sum(statuses.values())
            if requests:
                result.routes.append({"key": target.key, "requests": requests, "statuses": statuses})
        result.unreachable = errors_in_a_row >= MAX_CONSECUTIVE_ERRORS
        return result

    def url_for(self, path: str) -> str:
        return urllib.parse.urlunsplit((self.base.scheme, self.base.netloc, self.base.path.rstrip("/") + path, "", ""))

    @staticmethod
    def _spread_shortfall(targets: list, requests: Optional[dict]) -> None:
        """Requests the release is short of overall, spread across the routes,
        or sent to the home page when the plan names no route to send them to."""
        if not requests:
            return
        extra = int(requests.get("need") or 0) - int(requests.get("have") or 0) - sum(target.count for target in targets)
        if extra <= 0:
            return
        if not targets:
            targets.append(Target("GET /", "/", 0))
        for i in range(extra):
            targets[i % len(targets)].count += 1
