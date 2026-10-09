from __future__ import annotations

import django
from django.apps import AppConfig
from django.conf import settings

import deployangel

MIDDLEWARE = "deployangel.django.middleware.DeployAngelMiddleware"


class DeployAngelConfig(AppConfig):
    name = "deployangel.django"
    label = "deployangel"
    verbose_name = "DeployAngel"

    def ready(self) -> None:
        try:
            options = getattr(settings, "DEPLOYANGEL", None) or {}
            if options:
                deployangel.configure(**options)
            install_middleware()

            from deployangel.django import metadata, middleware, scheduled

            middleware.connect_signals()
            deployangel.add_metadata_source(metadata.DjangoRoutes())
            deployangel.add_metadata_source(scheduled.DjangoCrontab())
            _install_job_integrations()
            agent = deployangel.start(framework="django", framework_version=django.get_version(),
                                      debug=bool(getattr(settings, "DEBUG", False)))
            # Only where the agent reports: nothing is wrapped in development.
            if agent is not None and deployangel.recording():
                scheduled.install(agent.metadata.schedules())
        except Exception as error:
            deployangel.configuration().logger.warning("DeployAngel failed to start: %s: %s", type(error).__name__, error)


def install_middleware() -> None:
    """The middleware goes first, so it sees the final status after every other
    middleware and Django's own error pages. Settings name it once at most."""
    current = list(getattr(settings, "MIDDLEWARE", None) or [])
    if MIDDLEWARE not in current:
        settings.MIDDLEWARE = [MIDDLEWARE, *current]


def _install_job_integrations() -> None:
    try:
        import celery  # noqa: F401
    except ImportError:
        pass
    else:
        from deployangel import celery as celery_integration

        celery_integration.install()
