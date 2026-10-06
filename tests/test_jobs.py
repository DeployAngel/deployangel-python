from datetime import timedelta

import pytest
from conftest import drain

import deployangel
from deployangel.core import work


def jobs(payload):
    return {entry["key"]: entry for entry in payload["job_classes"]}


def checkpoints(payload):
    """key -> (count, recorded in a request, recorded in a job)"""
    return {entry["key"]: (entry["count"], entry["http"], entry["job"]) for entry in payload["checkpoints"]}


# Celery

celery = pytest.importorskip("celery")


@pytest.fixture
def celery_app():
    from celery import Celery

    app = Celery("shop", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = False

    @app.task(name="shop.tasks.send_receipt")
    def send_receipt(order_id):
        return order_id

    @app.task(name="shop.tasks.charge")
    def charge(order_id):
        deployangel.checkpoint("payment.attempted")
        raise ValueError(f"card for order {order_id} declined")

    @app.task(name="shop.tasks.fulfil")
    def fulfil(order_id):
        deployangel.checkpoint("order.fulfilled")
        return order_id

    @app.task(name="shop.tasks.sync", bind=True, max_retries=1)
    def sync(self):
        if self.request.retries == 0:
            raise self.retry(exc=ConnectionError("upstream timeout"), countdown=0)
        return "ok"

    return app


@pytest.fixture
def celery_agent(started_agent, celery_app):
    import deployangel.celery

    agent = started_agent(framework="celery")
    deployangel.celery._installed = False  # the Django tests' app config installed it already
    deployangel.celery.install(celery_app)
    yield agent
    deployangel.celery._installed = False
    deployangel.celery._app = None


def run_worker(app, *calls):
    from celery.contrib.testing.worker import start_worker

    with start_worker(app, pool="solo", perform_ping_check=False, shutdown_timeout=10):
        results = [call() for call in calls]
        for result in results:
            try:
                result.get(timeout=10)
            except Exception:
                pass


def test_celery_records_each_task_attempt_with_queue_latency(celery_agent, celery_app):
    run_worker(celery_app, lambda: celery_app.tasks["shop.tasks.send_receipt"].delay(1),
               lambda: celery_app.tasks["shop.tasks.send_receipt"].delay(2))
    payload = drain(celery_agent)
    receipt = jobs(payload)["shop.tasks.send_receipt"]
    assert (receipt["processed"], receipt["failed"], receipt["discarded"]) == (2, 0, 0)
    assert sum(receipt["queue_latency_histogram"]["counts"].values()) == 2
    assert "jobs" in payload["capabilities"]


def test_celery_failures_are_discarded_with_their_exception(celery_agent, celery_app):
    run_worker(celery_app, lambda: celery_app.tasks["shop.tasks.charge"].delay(7))
    payload = drain(celery_agent)
    charge = jobs(payload)["shop.tasks.charge"]
    assert (charge["processed"], charge["failed"], charge["discarded"]) == (1, 1, 1)
    (exception,) = payload["exceptions"]
    assert exception["exception_class"] == "ValueError"
    assert exception["message"] == "card for order <n> declined"
    assert exception["sources"] == {"job_class:shop.tasks.charge": 1}


def test_celery_a_retried_attempt_fails_without_being_discarded(celery_agent, celery_app):
    run_worker(celery_app, lambda: celery_app.tasks["shop.tasks.sync"].delay())
    payload = drain(celery_agent)
    sync = jobs(payload)["shop.tasks.sync"]
    assert (sync["processed"], sync["failed"], sync["discarded"]) == (2, 1, 0)
    assert payload["exceptions"][0]["exception_class"] == "ConnectionError"


def test_celery_checkpoints_count_as_recorded_in_a_job(celery_agent, celery_app):
    run_worker(celery_app, lambda: celery_app.tasks["shop.tasks.fulfil"].delay(1),
               lambda: celery_app.tasks["shop.tasks.charge"].delay(2))
    deployangel.checkpoint("order.fulfilled")
    assert checkpoints(drain(celery_agent)) == {"order.fulfilled": (2, 0, 1), "payment.attempted": (1, 0, 1)}


def test_celery_a_failed_eager_task_ends_its_job(celery_agent, celery_app):
    result = celery_app.tasks["shop.tasks.charge"].apply((7,))
    assert result.failed()
    assert work.current() is None
    deployangel.checkpoint("payment.attempted")
    assert checkpoints(drain(celery_agent)) == {"payment.attempted": (2, 0, 1)}


def test_celery_an_eager_retry_inside_its_attempt_ends_both(celery_agent, celery_app):
    """An eager retry can run inside the attempt it retries, under the same
    task id (Celery 5.3 does); each attempt ends its own job."""
    from deployangel import celery as integration

    task = celery_app.tasks["shop.tasks.fulfil"]
    integration._prerun(task_id="t1", task=task)
    integration._prerun(task_id="t1", task=task)
    deployangel.checkpoint("order.fulfilled")
    integration._postrun(task_id="t1", task=task, state="SUCCESS")
    integration._postrun(task_id="t1", task=task, state="RETRY")
    assert work.current() is None and integration._running == {}
    payload = drain(celery_agent)
    assert jobs(payload)["shop.tasks.fulfil"]["processed"] == 2
    assert checkpoints(payload) == {"order.fulfilled": (1, 0, 1)}


def test_celery_a_task_run_eagerly_inside_a_request_counts_as_a_job(celery_agent, celery_app):
    pytest.importorskip("flask")
    from flask import Flask

    import deployangel.flask

    app = Flask(__name__)

    @app.post("/orders")
    def create_order():
        deployangel.checkpoint("order.created")
        celery_app.tasks["shop.tasks.fulfil"].apply((1,))
        celery_app.tasks["shop.tasks.fulfil"].delay(2)  # task_always_eager
        deployangel.checkpoint("order.created")
        return {"id": 1}, 201

    deployangel.flask.init_app(app)
    celery_app.conf.task_always_eager = True
    assert app.test_client().post("/orders").status_code == 201
    payload = drain(celery_agent)
    assert checkpoints(payload) == {"order.created": (2, 2, 0), "order.fulfilled": (2, 0, 2)}
    assert jobs(payload)["shop.tasks.fulfil"]["processed"] == 2


def test_celery_lists_tasks_and_beat_schedules(celery_agent, celery_app):
    from celery.schedules import crontab

    from deployangel.celery import CelerySource

    celery_app.conf.timezone = "Europe/Berlin"
    celery_app.conf.beat_schedule = {
        "nightly receipts": {"task": "shop.tasks.send_receipt", "schedule": crontab(minute=30, hour="2,14")},
        "weekday sync": {"task": "shop.tasks.sync", "schedule": crontab(minute="*/15", day_of_week="mon-fri")},
        "every five minutes": {"task": "shop.tasks.sync", "schedule": 300.0},
        "hourly": {"task": "shop.tasks.sync", "schedule": timedelta(hours=1)},
        "custom": {"task": "shop.tasks.sync", "schedule": object()},
    }
    source = CelerySource()
    assert set(source.job_classes()) == {"shop.tasks.send_receipt", "shop.tasks.charge", "shop.tasks.sync",
                                         "shop.tasks.fulfil"}
    schedules = {s["key"]: s for s in source.schedules()}
    assert schedules["nightly receipts"] == {"key": "nightly receipts", "class": "shop.tasks.send_receipt",
                                             "source": "celery_beat", "time_zone": "Europe/Berlin", "schedule": "30 2,14 * * *"}
    assert schedules["weekday sync"]["schedule"] == "*/15 * * * mon-fri"
    assert (schedules["every five minutes"]["schedule"], schedules["every five minutes"]["every"]) == (None, "300s")
    assert schedules["hourly"]["every"] == "3600s"
    assert "custom" not in schedules


def test_celery_time_zone_is_utc_by_default(celery_app):
    from deployangel.celery import time_zone_name

    assert time_zone_name(celery_app) == "UTC"


# RQ

rq = pytest.importorskip("rq")
fakeredis = pytest.importorskip("fakeredis")


def receipt(order_id):
    return order_id


def failing_charge(order_id):
    raise ValueError(f"card for order {order_id} declined")


def fulfil(order_id):
    deployangel.checkpoint("order.fulfilled")
    if order_id < 0:
        raise ValueError("no such order")
    return order_id


@pytest.fixture
def rq_queue():
    from rq import Queue

    return Queue("default", connection=fakeredis.FakeRedis())


def test_rq_records_jobs_and_parses_failures_from_tracebacks(started_agent, rq_queue):
    from rq import Retry

    import deployangel.rq

    agent = started_agent(framework="rq")
    rq_queue.enqueue(receipt, 1)
    rq_queue.enqueue(failing_charge, 7)
    rq_queue.enqueue(failing_charge, 8, retry=Retry(max=1))
    worker = deployangel.rq.SimpleWorker([rq_queue], connection=rq_queue.connection)
    worker.work(burst=True)

    payload = drain(agent)
    table = jobs(payload)
    assert (table["test_jobs.receipt"]["processed"], table["test_jobs.receipt"]["failed"]) == (1, 0)
    charge = table["test_jobs.failing_charge"]
    # Two discarded failures, and one attempt retried before it failed again.
    assert (charge["processed"], charge["failed"], charge["discarded"]) == (3, 3, 2)
    (exception,) = payload["exceptions"]
    assert exception["exception_class"] == "ValueError"
    assert exception["message"] == "card for order <n> declined"
    assert exception["top_frame"] == "tests/test_jobs.py#failing_charge"
    assert exception["sources"] == {"job_class:test_jobs.failing_charge": 2}
    assert "jobs" in payload["capabilities"]


def test_rq_checkpoints_count_as_recorded_in_a_job(started_agent, rq_queue):
    import deployangel.rq

    agent = started_agent(framework="rq")
    rq_queue.enqueue(fulfil, 1)
    rq_queue.enqueue(fulfil, -1)
    deployangel.rq.SimpleWorker([rq_queue], connection=rq_queue.connection).work(burst=True)
    assert work.current() is None
    deployangel.checkpoint("order.fulfilled")
    payload = drain(agent)
    assert checkpoints(payload) == {"order.fulfilled": (3, 0, 2)}
    assert jobs(payload)["test_jobs.fulfil"]["failed"] == 1


def test_rq_forking_worker_records_from_the_parent_after_each_horse(started_agent):
    """RQ's default worker runs each job in a forked horse that exits with
    os._exit. Needs a real Redis: RQ_REDIS_URL=redis://localhost:6379/15."""
    import os
    import uuid

    url = os.environ.get("RQ_REDIS_URL")
    if not url:
        pytest.skip("set RQ_REDIS_URL to run against a real Redis")
    from redis import Redis
    from rq import Queue

    import deployangel.rq

    agent = started_agent(framework="rq")
    connection = Redis.from_url(url)
    queue = Queue(f"deployangel-test-{uuid.uuid4().hex[:8]}", connection=connection)
    jobs_enqueued = []
    worker = deployangel.rq.Worker([queue], connection=connection)
    try:
        jobs_enqueued = [queue.enqueue(receipt, 1), queue.enqueue(failing_charge, 9)]
        worker.work(burst=True)
        payload = drain(agent)
        table = jobs(payload)
        assert (table["test_jobs.receipt"]["processed"], table["test_jobs.receipt"]["failed"]) == (1, 0)
        assert (table["test_jobs.failing_charge"]["failed"], table["test_jobs.failing_charge"]["discarded"]) == (1, 1)
        assert payload["exceptions"][0]["top_frame"] == "tests/test_jobs.py#failing_charge"
    finally:
        # Leave nothing behind in the Redis database the test borrowed.
        keys = [key for key in connection.scan_iter(f"*{queue.name}*")]
        keys += [f"rq:{kind}:{job.id}" for job in jobs_enqueued for kind in ("job", "results", "executions")]
        connection.delete(*keys, worker.key)
        for registry, member in (("rq:queues", queue.key), ("rq:workers", worker.key)):
            connection.srem(registry, member)
            if not connection.smembers(registry):
                connection.delete(registry)
