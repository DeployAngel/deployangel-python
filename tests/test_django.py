import pytest

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402

try:
    import django_crontab  # noqa: F401
    CRONTAB_APPS = ["django_crontab"]
except ImportError:
    CRONTAB_APPS = []

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF="django_app.urls",
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "rest_framework", "django_celery_beat",
                        *CRONTAB_APPS, "deployangel.django"],
        CRONJOBS=[("30 3 * * *", "django_app.cron.nightly_cleanup", [5]), ("0 * * * *", "django_app.cron.failing_job"),
                  ("15 4 * * 1", "django.core.management.call_command", ["clearsessions"])],
        MIDDLEWARE=["django.middleware.common.CommonMiddleware"],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}},
        USE_TZ=True,
        TIME_ZONE="America/New_York",
        DEPLOYANGEL={"ignored_routes": ["GET /ignored/"]},
    )
    django.setup()

from conftest import drain  # noqa: E402
from django.test import AsyncClient, Client  # noqa: E402

import deployangel  # noqa: E402
from deployangel.django.metadata import DjangoRoutes  # noqa: E402


@pytest.fixture
def agent(started_agent):
    agent = started_agent(framework="django")
    deployangel.add_metadata_source(DjangoRoutes())
    return agent


def routes(payload):
    return {route["key"]: route for route in payload["routes"]}


def test_the_app_config_puts_the_middleware_first_and_reads_settings():
    assert settings.MIDDLEWARE[0] == "deployangel.django.middleware.DeployAngelMiddleware"
    assert settings.MIDDLEWARE.count("deployangel.django.middleware.DeployAngelMiddleware") == 1


def test_records_requests_against_the_resolved_url_pattern(agent):
    client = Client()
    client.get("/products/1/")
    client.get("/products/2/")
    client.post("/orders/7/")
    client.get("/api/report/")
    client.get("/api/v2/items/9/")
    payload = drain(agent)
    assert set(routes(payload)) == {"GET /products/<int:pk>/", "POST /orders/<int:pk>/", "GET /api/report/",
                                    "GET /api/v2/items/<pk>/"}
    assert routes(payload)["GET /products/<int:pk>/"]["requests"] == 2
    assert payload["http"]["requests"] == 5


def test_a_view_exception_is_an_unhandled_500_with_its_route(agent):
    Client(raise_request_exception=False).get("/checkout/", HTTP_HOST="shop.example.com")
    payload = drain(agent)
    assert payload["http"]["status_counts"] == {"500": 1}
    assert payload["http"]["unhandled_exceptions"] == 1
    (exception,) = payload["exceptions"]
    assert exception["exception_class"] == "ValueError"
    assert exception["message"] == "card <n> declined"
    assert exception["sources"] == {"route:GET /checkout/": 1}
    assert exception["top_frame"] == "tests/django_app/views.py#checkout"


def test_unmatched_paths_and_health_checks_stay_out_of_the_totals(agent):
    client = Client()
    client.get("/wp-admin/")
    client.get("/healthz/")
    payload = drain(agent)
    assert payload["http"]["requests"] == 0
    assert set(routes(payload)) == {"GET unmatched"}


async def test_records_requests_served_under_asgi(agent):
    await AsyncClient().get("/async/3/")
    assert set(routes(drain(agent))) == {"GET /async/<int:pk>/"}


def checkpoints(payload):
    return {entry["key"]: (entry["count"], entry["http"], entry["job"]) for entry in payload["checkpoints"]}


def test_checkpoints_count_as_recorded_in_a_request(agent):
    from deployangel.core import work

    Client().post("/place-order/")
    Client(raise_request_exception=False).post("/place-order/?fail=1")
    with pytest.raises(ValueError):
        Client().post("/place-order/?fail=1")
    assert work.current() is None
    deployangel.checkpoint("order.created")
    assert checkpoints(drain(agent)) == {"order.created": (4, 3, 0)}


async def test_checkpoints_count_as_recorded_in_a_request_under_asgi(agent):
    await AsyncClient().post("/async/place-order/")
    await AsyncClient().post("/place-order/")  # a sync view, run in a thread
    assert checkpoints(drain(agent)) == {"order.created": (2, 2, 0)}


def test_lists_routes_with_their_methods_views_and_files(agent):
    table = {route["key"]: route for route in DjangoRoutes().routes()}
    # A plain function view's methods can't be told, so it's listed under ANY.
    assert table["ANY /products/<int:pk>/"] == {"key": "ANY /products/<int:pk>/", "controller": "django_app.views",
                                                "action": "product", "files": ["tests/django_app/views.py"]}
    assert {"ANY /checkout/", "GET /orders/<int:pk>/", "POST /orders/<int:pk>/", "GET /api/report/", "POST /api/report/",
            "GET /api/v2/items/", "GET /api/v2/items/<pk>/", "ANY /api/legacy/<slug>/"} <= set(table)
    assert "GET /products/<int:pk>/" not in table
    assert table["GET /orders/<int:pk>/"]["action"] == "OrderView"
    assert table["GET /api/v2/items/"]["action"] == "ItemViewSet"


def test_reads_django_celery_beat_periodic_tasks(agent):
    from django.core.management import call_command
    from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

    from deployangel.celery import django_celery_beat_schedules

    call_command("migrate", "django_celery_beat", verbosity=0)
    crontab = CrontabSchedule.objects.create(minute="15", hour="4", timezone="Europe/London")
    interval = IntervalSchedule.objects.create(every=10, period=IntervalSchedule.MINUTES)
    PeriodicTask.objects.create(name="nightly report", task="shop.tasks.report", crontab=crontab)
    PeriodicTask.objects.create(name="sync", task="shop.tasks.sync", interval=interval)
    PeriodicTask.objects.create(name="off", task="shop.tasks.off", interval=interval, enabled=False)

    schedules = {s["key"]: s for s in django_celery_beat_schedules("America/New_York")}
    assert schedules["nightly report"] == {"key": "nightly report", "class": "shop.tasks.report", "source": "django_celery_beat",
                                           "time_zone": "Europe/London", "schedule": "15 4 * * *"}
    assert schedules["sync"] == {"key": "sync", "class": "shop.tasks.sync", "source": "django_celery_beat",
                                 "time_zone": "America/New_York", "schedule": None, "every": "600s"}
    assert "off" not in schedules


# Scheduled work outside Celery: django-crontab and management commands

def test_reads_django_crontab_jobs_as_schedules_in_the_servers_zone(agent, monkeypatch):
    pytest.importorskip("django_crontab")
    from deployangel.django.scheduled import DjangoCrontab

    monkeypatch.setenv("TZ", "UTC")
    assert DjangoCrontab().schedules() == [
        {"key": "django_app.cron.nightly_cleanup", "class": "django_app.cron.nightly_cleanup", "schedule": "30 3 * * *",
         "source": "django_crontab", "time_zone": "UTC"},
        {"key": "django_app.cron.failing_job", "class": "django_app.cron.failing_job", "schedule": "0 * * * *",
         "source": "django_crontab", "time_zone": "UTC"},
        {"key": "manage.py clearsessions", "class": "manage.py clearsessions", "schedule": "15 4 * * 1",
         "source": "django_crontab", "time_zone": "UTC"},
    ]


def test_records_each_run_of_a_django_crontab_job_the_way_it_calls_them(agent):
    pytest.importorskip("django_crontab")
    import importlib

    from django_app import cron

    from deployangel.django import scheduled

    scheduled.install([])
    scheduled.install([])  # wraps once
    # django-crontab looks the function up by name when the job runs.
    module = importlib.import_module("django_app.cron")
    assert getattr(module, "nightly_cleanup")(limit=5) == 5
    with pytest.raises(RuntimeError):
        getattr(module, "failing_job")()
    jobs = {entry["key"]: entry for entry in drain(agent)["job_classes"]}
    assert (jobs["django_app.cron.nightly_cleanup"]["processed"], jobs["django_app.cron.nightly_cleanup"]["failed"]) == (1, 0)
    assert jobs["django_app.cron.failing_job"]["failed"] == 1
    assert cron.ran == [5]


def test_records_management_commands_a_schedule_names_and_no_others(agent, monkeypatch):
    from django.core.management import call_command

    from deployangel.django import scheduled

    monkeypatch.setattr(scheduled, "_scheduled_commands", set())
    scheduled.install([{"class": "manage.py check"}])
    call_command("check")
    call_command("diffsettings")
    jobs = {entry["key"] for entry in drain(agent)["job_classes"]}
    assert "manage.py check" in jobs
    assert "manage.py diffsettings" not in jobs
