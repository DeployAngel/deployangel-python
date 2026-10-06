"""Recording a finished HTTP request, the same for every framework. An
adapter (Django, Starlette/FastAPI, Flask) measures the request, names the
route pattern the framework matched, and hands both here.

A request is only counted against a route pattern the framework matched,
never the raw path, which keeps IDs out of route keys."""

from __future__ import annotations

import time
from typing import Optional

import deployangel

# Health checks at a conventional path. Load balancers and uptime monitors
# call them all the time and they answer fast, so counting them would make any
# app look busy and healthy. Others can be left out with ignored_routes.
HEALTH_CHECK_PATHS = ("up", "health", "healthz", "healthcheck", "health_check", "livez", "readyz", "statusz", "ping",
                      "ht", "alive")


def is_placeholder(segment: str) -> bool:
    """A path segment that matches many values: Django's and Flask's <id>,
    Starlette's {id}, a regex group, or :id and *path."""
    return segment.startswith((":", "*")) or any(char in segment for char in "<{(")


def is_health_check_path(pattern: Optional[str]) -> bool:
    """A static route ending in a health-check name, such as "/healthz/" or
    "/api/livez". A route with a placeholder, such as "/patients/<id>/health",
    is a real page about something, so it's recorded."""
    if not pattern:
        return False
    segments = [segment for segment in pattern.strip("^$").split("/") if segment]
    return bool(segments) and segments[-1] in HEALTH_CHECK_PATHS and not any(is_placeholder(s) for s in segments)


def record(method: str, pattern: Optional[str], status: int, started: float, unhandled: bool = False,
           exception: Optional[BaseException] = None, health_check: Optional[bool] = None) -> None:
    """started is a time.perf_counter() reading from when the request began.
    health_check, when the adapter knows better than the path (a health-check
    view), overrides the path rule."""
    try:
        if (is_health_check_path(pattern) if health_check is None else health_check):
            return
        status = int(status)
        route = f"{method} {pattern}" if pattern else None
        if route and deployangel.configuration().is_ignored_route(route):
            return
        # Unrouted successes are static files and similar; unrouted errors
        # (such as 404s) are still recorded.
        if route is None and status < 400:
            return
        key = route or f"{method} unmatched"
        if exception is not None:
            deployangel.record_exception(exception, source=f"route:{key}")
        deployangel.record_request(
            route_key=key,
            status=status,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            unhandled=unhandled,
            # A 4xx no route matched is mostly bots probing paths like
            # /wp-admin. It stays visible under "unmatched", but out of the
            # app's totals, so it doesn't add to the evidence or dilute real
            # pages' latency. An unrouted 5xx still counts: something broke.
            in_totals=route is not None or status >= 500,
        )
    except Exception:
        pass
