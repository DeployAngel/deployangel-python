"""Records each request against the URL pattern Django resolved. Works under
WSGI and ASGI without forcing a sync/async switch."""

from __future__ import annotations

import time
from typing import Optional

from asgiref.sync import iscoroutinefunction, markcoroutinefunction
from django.core.signals import got_request_exception

import deployangel
from deployangel import http
from deployangel.core import redaction, work

EXCEPTION = "_deployangel_exception"
# Health checks served by django-health-check, django-alive, and
# django-watchman, wherever they're mounted.
HEALTH_CHECK_MODULES = ("health_check.", "django_alive.", "watchman.")


class DeployAngelMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response):
        self.get_response = get_response
        if iscoroutinefunction(get_response):
            markcoroutinefunction(self)

    def __call__(self, request):
        if iscoroutinefunction(self):
            return self.__acall__(request)
        if not deployangel.recording():
            return self.get_response(request)
        started = time.perf_counter()
        redaction.set_request_host(request.META.get("HTTP_HOST"))
        unit = work.begin(work.HTTP)
        try:
            response = self.get_response(request)
        finally:
            work.end(unit)
        _record(request, response, started)
        return response

    async def __acall__(self, request):
        if not deployangel.recording():
            return await self.get_response(request)
        started = time.perf_counter()
        redaction.set_request_host(request.META.get("HTTP_HOST"))
        unit = work.begin(work.HTTP)
        try:
            response = await self.get_response(request)
        finally:
            work.end(unit)
        _record(request, response, started)
        return response


def _record(request, response, started: float) -> None:
    try:
        status = int(getattr(response, "status_code", 500))
        match = getattr(request, "resolver_match", None)
        # Django renders every exception as a response before it reaches this
        # middleware; got_request_exception saved the ones that became a 5xx.
        exception = getattr(request, EXCEPTION, None) if status >= 500 else None
        http.record(request.method, route_pattern(match), status, started, unhandled=exception is not None,
                    exception=exception, health_check=_health_check(match))
    except Exception:
        pass


def route_pattern(match) -> Optional[str]:
    """The matched route as Django joins it ("users/<int:pk>/"), with a leading
    slash; regex patterns keep their groups, without the ^ and $ anchors."""
    route = getattr(match, "route", None)
    if route is None:
        return None
    return normalize_route(str(route))


def normalize_route(route: str) -> str:
    """Regex routes read like path routes: a named group is shown by its name,
    so DRF's "^items/(?P<pk>[^/.]+)/$" is "/items/<pk>/"."""
    route = route.removeprefix("^")
    for anchor in ("\\Z", "$"):
        route = route.removesuffix(anchor)
    return "/" + _name_groups(route)


def _name_groups(route: str) -> str:
    out = []
    i = 0
    while i < len(route):
        if route.startswith("(?P<", i) and route.find(">", i) > 0:
            end = _group_end(route, i)
            if end is None:
                break
            out.append("<" + route[i + 4:route.index(">", i)] + ">")
            i = end + 1
        else:
            out.append(route[i])
            i += 1
    return "".join(out) + route[i:]


def _group_end(route: str, start: int):
    """The index of the ")" closing the group opened at start."""
    depth = 0
    i = start
    in_class = False
    while i < len(route):
        char = route[i]
        if char == "\\":
            i += 2
            continue
        if in_class:
            in_class = char != "]"
        elif char == "[":
            in_class = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _health_check(match) -> Optional[bool]:
    func = getattr(match, "func", None)
    if func is None:
        return None
    view = getattr(func, "view_class", None) or getattr(func, "cls", None) or func
    module = getattr(view, "__module__", "") or ""
    if module.startswith(HEALTH_CHECK_MODULES):
        return True
    return None


def _save_exception(sender, request=None, **kwargs) -> None:
    import sys

    if request is not None:
        try:
            setattr(request, EXCEPTION, sys.exc_info()[1])
        except Exception:
            pass


def connect_signals() -> None:
    got_request_exception.connect(_save_exception, dispatch_uid="deployangel.save_exception")
