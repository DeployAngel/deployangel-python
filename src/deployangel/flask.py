"""Flask integration: records each request against the URL rule Flask matched
("GET /users/<int:user_id>"), and lists the app's rules.

    app = Flask(__name__)
    deployangel.flask.init_app(app)
"""

from __future__ import annotations

import time
from typing import Optional

import deployangel
from deployangel import http
from deployangel.core import redaction, work
from deployangel.metadata import module_file

ROUTE = "deployangel.route"
EXCEPTION = "deployangel.exception"
IGNORED_METHODS = ("HEAD", "OPTIONS")


def init_app(app):
    try:
        import flask
        from flask import got_request_exception, request_started

        request_started.connect(_on_request_started, app, weak=False)
        got_request_exception.connect(_on_exception, app, weak=False)
        app.wsgi_app = WsgiMiddleware(app.wsgi_app)
        deployangel.add_metadata_source(FlaskRoutes(app))
        version = _version(flask)
        return deployangel.start(framework="flask", framework_version=version, debug=bool(app.debug))
    except Exception as error:
        deployangel.configuration().logger.warning("DeployAngel failed to start: %s: %s", type(error).__name__, error)
        return None


class WsgiMiddleware:
    """Wraps Flask's own WSGI app, so it sees the final status, including
    Flask's error pages. The matched rule and any unhandled exception are
    noted in the WSGI environ by Flask's signals."""

    def __init__(self, wsgi_app):
        self.wsgi_app = wsgi_app

    def __call__(self, environ, start_response):
        if not deployangel.recording():
            return self.wsgi_app(environ, start_response)
        started = time.perf_counter()
        redaction.set_request_host(environ.get("HTTP_HOST"))
        status = []

        def start_response_with_status(status_line, headers, exc_info=None):
            status.append(status_line)
            return start_response(status_line, headers, exc_info)

        unit = work.begin(work.HTTP)
        try:
            response = self.wsgi_app(environ, start_response_with_status)
        except Exception as exception:
            _record(environ, 500, started, exception)
            raise
        finally:
            work.end(unit)
        code = _status_code(status[-1]) if status else 500
        _record(environ, code, started, environ.get(EXCEPTION) if code >= 500 else None)
        return response


def _record(environ, status: int, started: float, exception: Optional[BaseException]) -> None:
    http.record(environ.get("REQUEST_METHOD", "GET"), environ.get(ROUTE), status, started,
                unhandled=exception is not None, exception=exception)


def _status_code(status_line: str) -> int:
    try:
        return int(str(status_line).split(" ", 1)[0])
    except ValueError:
        return 500


def _on_request_started(sender, **kwargs) -> None:
    try:
        from flask import request

        rule = request.url_rule
        if rule is not None:
            request.environ[ROUTE] = rule.rule
    except Exception:
        pass


def _on_exception(sender, exception=None, **kwargs) -> None:
    try:
        from flask import request

        request.environ[EXCEPTION] = exception
    except Exception:
        pass


def _version(flask) -> Optional[str]:
    try:
        from importlib.metadata import version

        return version("flask")
    except Exception:
        return getattr(flask, "__version__", None)


class FlaskRoutes:
    def __init__(self, app):
        self.app = app

    def routes(self) -> list:
        agent = deployangel.agent()
        root = agent.root if agent else None
        routes = []
        for rule in self.app.url_map.iter_rules():
            if rule.endpoint == "static" or rule.endpoint.endswith(".static"):
                continue
            view = self.app.view_functions.get(rule.endpoint)
            view = getattr(view, "view_class", None) or view
            module = getattr(view, "__module__", None) or ""
            entry = {"controller": module, "action": getattr(view, "__qualname__", None) or rule.endpoint}
            source = module_file(module, root)
            if source:
                entry["files"] = [source]
            for method in sorted(rule.methods or ()):
                if method not in IGNORED_METHODS:
                    routes.append(dict(entry, key=f"{method} {rule.rule}"))
        return routes
