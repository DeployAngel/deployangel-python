"""ASGI integration for Starlette and FastAPI: a middleware that records each
request against the route the router matched ("GET /items/{item_id}"), and
the route table for the metadata.

    app = FastAPI()
    deployangel.fastapi.init(app)
"""

from __future__ import annotations

import time
from typing import Optional

import deployangel
from deployangel import http
from deployangel.core import redaction, work
from deployangel.metadata import module_file

IGNORED_METHODS = ("HEAD", "OPTIONS")
# The documentation pages FastAPI serves itself.
FRAMEWORK_MODULES = ("fastapi.", "starlette.")


def init(app):
    """Adds the middleware, lists the app's routes, and starts the agent. Call
    it where the app is created, before it serves requests."""
    try:
        app.add_middleware(DeployAngelMiddleware)
        deployangel.add_metadata_source(StarletteRoutes(app))
        framework, version = _framework(app)
        return deployangel.start(framework=framework, framework_version=version, debug=bool(getattr(app, "debug", False)))
    except Exception as error:
        deployangel.configuration().logger.warning("DeployAngel failed to start: %s: %s", type(error).__name__, error)
        return None


class DeployAngelMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not deployangel.recording():
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        # The router changes the scope as it goes (a mount moves root_path,
        # and a mounted app replaces "app"), so note what it was.
        original = {"type": "http", "path": scope.get("path", ""), "root_path": scope.get("root_path", ""),
                    "method": scope.get("method", "GET"), "headers": scope.get("headers", [])}
        outer_app = scope.get("app")
        redaction.set_request_host(_header(scope, b"host"))
        status = []

        async def send_with_status(message):
            if message.get("type") == "http.response.start":
                status.append(message.get("status", 200))
            await send(message)

        unit = work.begin(work.HTTP)
        try:
            await self.app(scope, receive, send_with_status)
        except Exception as exception:
            _record(scope, original, outer_app, 500, started, exception)
            raise
        finally:
            work.end(unit)
        _record(scope, original, outer_app, status[0] if status else 500, started, None)


def _record(scope, original, outer_app, status, started, exception) -> None:
    try:
        http.record(original["method"], route_pattern(scope, original, outer_app), status, started,
                    unhandled=exception is not None, exception=exception)
    except Exception:
        pass


def route_pattern(scope, original, outer_app) -> Optional[str]:
    """The matched route's path format, with the paths of any mounts it sits
    under; None when nothing matched or a mounted non-router app (such as
    StaticFiles) answered."""
    from starlette.routing import Mount

    route = scope.get("route")
    if route is None:
        if scope.get("endpoint") is None:
            return None
        route = _match(getattr(outer_app, "routes", []), original)
        return route
    if isinstance(route, Mount):
        return None
    pattern = _effective_path(scope) or getattr(route, "path_format", None) or getattr(route, "path", None)
    if not pattern:
        return None
    if scope.get("root_path", "") != original["root_path"]:
        pattern = _mount_prefix(getattr(outer_app, "routes", []), _route_path(original)) + pattern
    return pattern


def _effective_path(scope) -> Optional[str]:
    """FastAPI 0.14x includes routers lazily: the route in the scope is the
    router's own, without the prefix include_router() added, and the path the
    request matched is in FastAPI's part of the scope."""
    fastapi_scope = scope.get("fastapi")
    context = fastapi_scope.get("effective_route_context") if isinstance(fastapi_scope, dict) else None
    if context is None:
        return None
    return getattr(getattr(context, "starlette_route", None), "path_format", None) or getattr(context, "path_format", None)


def _route_path(scope) -> str:
    path, root = scope["path"], scope["root_path"]
    return path[len(root):] if root and path.startswith(root) else path


def _mount_prefix(routes, path: str) -> str:
    from starlette.routing import Mount

    for route in routes:
        if isinstance(route, Mount):
            match = route.path_regex.match(path)
            if match:
                return route.path + _mount_prefix(getattr(route, "routes", []) or [], "/" + match.groupdict().get("path", ""))
    return ""


def _match(routes, scope, prefix: str = "") -> Optional[str]:
    """For a router that doesn't name the matched route in the scope: matches
    the request again against the route table."""
    from starlette.routing import Match, Mount

    for route in routes:
        try:
            if isinstance(route, Mount):
                match = route.path_regex.match(_route_path(scope))
                if match and getattr(route, "routes", None):
                    child = dict(scope, path="/" + match.groupdict().get("path", ""), root_path="")
                    found = _match(route.routes, child, prefix + route.path)
                    if found:
                        return found
                continue
            result, _ = route.matches(scope)
            if result == Match.FULL:
                return prefix + (getattr(route, "path_format", None) or route.path)
        except Exception:
            continue
    return None


def _header(scope, name: bytes) -> Optional[str]:
    for key, value in scope.get("headers") or []:
        if key == name:
            return value.decode("latin-1")
    return None


def _framework(app) -> tuple:
    try:
        import fastapi

        if isinstance(app, fastapi.FastAPI):
            return "fastapi", fastapi.__version__
    except ImportError:
        pass
    import starlette

    return "starlette", getattr(starlette, "__version__", None)


class StarletteRoutes:
    def __init__(self, app):
        self.app = app

    def routes(self) -> list:
        agent = deployangel.agent()
        root = agent.root if agent else None
        return list(_walk(self.app.routes, "", root))


def _walk(routes, prefix: str, root: Optional[str]):
    from starlette.routing import Mount

    for route in _flatten(routes):
        if isinstance(route, Mount):
            if getattr(route, "routes", None):
                yield from _walk(route.routes, prefix + route.path, root)
            continue
        if not getattr(route, "methods", None) or not getattr(route, "endpoint", None):
            continue
        endpoint = getattr(route, "endpoint", None)
        module = getattr(endpoint, "__module__", None) or ""
        if module.startswith(FRAMEWORK_MODULES):
            continue
        path = prefix + (getattr(route, "path_format", None) or route.path)
        entry = {"controller": module, "action": getattr(endpoint, "__qualname__", None) or getattr(endpoint, "__name__", "")}
        source = module_file(module, root)
        if source:
            entry["files"] = [source]
        for method in sorted(getattr(route, "methods", None) or ["GET"]):
            if method not in IGNORED_METHODS:
                yield dict(entry, key=f"{method} {path}")


def _flatten(routes):
    """The routes as matched, with FastAPI 0.14x's lazily included routers
    expanded (each with its include_router() prefix)."""
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:
        return list(routes)
    from starlette.routing import Mount

    flat = []
    for route in routes:
        if isinstance(route, Mount):
            flat.append(route)
        else:
            flat.extend(iter_route_contexts([route]))
    return flat
