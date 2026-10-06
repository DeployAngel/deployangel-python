"""The route table from Django's URL resolver, keyed the way the middleware
records requests ("GET /users/<int:pk>/"), with the view and its file."""

from __future__ import annotations

import deployangel
from deployangel.metadata import module_file

# Admin, static file, and debug views: framework pages rather than the app's
# own. Requests to them are still recorded; they're just not listed as routes
# a release should exercise.
FRAMEWORK_MODULES = ("django.contrib.admin", "django.contrib.staticfiles", "django.views.static", "debug_toolbar",
                     "health_check.", "django_alive.", "watchman.")
# A view whose methods can't be told (a plain function view) is listed once,
# under ANY, so DeployAngel still knows which file serves the path.
ANY = "ANY"


class DjangoRoutes:
    def routes(self) -> list:
        from django.urls import get_resolver

        from deployangel.django.middleware import normalize_route

        agent = deployangel.agent()
        root = agent.root if agent else None
        routes = []
        for route, callback in walk(get_resolver().url_patterns):
            view = getattr(callback, "view_class", None) or getattr(callback, "cls", None) or callback
            module = getattr(view, "__module__", None) or ""
            if module.startswith(FRAMEWORK_MODULES):
                continue
            path = normalize_route(route)
            entry = {"controller": module, "action": getattr(view, "__qualname__", None) or getattr(view, "__name__", "")}
            source = module_file(module, root)
            if source:
                entry["files"] = [source]
            routes.extend(dict(entry, key=f"{method} {path}") for method in methods(callback))
        return routes


def walk(patterns, prefix: str = ""):
    """(route, callback) for every URL pattern, with routes joined as Django's
    resolver joins them for request.resolver_match.route."""
    from django.urls import URLPattern, URLResolver

    for pattern in patterns:
        try:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern.url_patterns, join_route(prefix, str(pattern.pattern)))
            elif isinstance(pattern, URLPattern):
                yield join_route(prefix, str(pattern.pattern)), pattern.callback
        except Exception:
            continue


def join_route(route1: str, route2: str) -> str:
    if not route1:
        return route2
    return route1 + route2.removeprefix("^")


def methods(callback) -> list:
    """The HTTP methods a view answers: a Django REST framework viewset's
    actions, else the handlers a class-based view (or @api_view) defines."""
    actions = getattr(callback, "actions", None)
    if isinstance(actions, dict) and actions:
        return sorted(method.upper() for method in actions if method.lower() not in ("head", "options"))
    view_class = getattr(callback, "view_class", None) or getattr(callback, "cls", None)
    if view_class is not None:
        names = getattr(view_class, "http_method_names", ())
        found = sorted(name.upper() for name in names if name not in ("head", "options") and hasattr(view_class, name))
        if found:
            return found
    return [ANY]
