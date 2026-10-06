import pytest

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=False,
        SECRET_KEY="test",
        ALLOWED_HOSTS=["*"],
        ROOT_URLCONF="django_app.urls",
        INSTALLED_APPS=["django.contrib.contenttypes", "django.contrib.auth", "rest_framework", "django_celery_beat",
                        "deployangel.django"],
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
