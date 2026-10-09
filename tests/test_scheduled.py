"""Scheduled work outside the app's job system: deployangel.task() and
config.recurring_jobs. Standard library only, like the agent itself."""

import pytest
from conftest import drain

import deployangel
from deployangel.core import work
from deployangel.scheduled import ConfiguredSchedules, local_time_zone, manage_commands


def job_classes(payload):
    return {entry["key"]: entry for entry in payload["job_classes"]}


def test_a_task_is_recorded_as_a_job_run_under_its_name(started_agent):
    agent = started_agent()
    seen = []
    with deployangel.task("nightly import"):
        seen.append(work.current())
        deployangel.checkpoint("rows.imported", count=3)
    assert seen == [work.JOB]
    assert work.current() is None
    payload = drain(agent)
    entry = job_classes(payload)["nightly import"]
    assert (entry["processed"], entry["failed"]) == (1, 0)
    assert "jobs" in payload["capabilities"]
    assert payload["checkpoints"] == [{"key": "rows.imported", "count": 3, "http": 0, "job": 3}]


def test_a_decorated_function_records_each_call_and_returns_its_value(started_agent):
    agent = started_agent()

    @deployangel.task("invoices:send")
    def send(count):
        return count * 2

    assert send.__name__ == "send"
    assert (send(2), send(3)) == (4, 6)
    assert job_classes(drain(agent))["invoices:send"]["processed"] == 2


def test_a_failing_task_is_recorded_and_its_exception_re_raised(started_agent):
    agent = started_agent()
    with pytest.raises(ValueError, match="bad row"):
        with deployangel.task("nightly import"):
            raise ValueError("bad row")
    payload = drain(agent)
    assert job_classes(payload)["nightly import"]["failed"] == 1
    (exception,) = payload["exceptions"]
    assert (exception["exception_class"], exception["sources"]) == ("ValueError", {"job_class:nightly import": 1})


def test_a_task_just_runs_when_the_agent_isnt_recording_or_the_name_cant_be_used(started_agent):
    with deployangel.task("not started"):
        pass
    agent = started_agent()
    for name in ("", " leading space", "x" * 201):
        with deployangel.task(name):
            pass
    assert drain(agent) is None or not drain(agent)["job_classes"]


def test_recurring_jobs_are_sent_as_declared_schedules_in_the_servers_zone(started_agent, monkeypatch):
    monkeypatch.setenv("TZ", "America/Chicago")
    agent = started_agent(recurring_jobs={"shop.tasks.sync": "*/15 * * * *", "manage.py send_invoices": "every day at 4am",
                                          "blank": " "})
    assert agent.metadata.schedules() == [
        {"key": "shop.tasks.sync", "class": "shop.tasks.sync", "schedule": "*/15 * * * *", "source": "config",
         "time_zone": "America/Chicago"},
        {"key": "manage.py send_invoices", "class": "manage.py send_invoices", "schedule": "every day at 4am",
         "source": "config", "time_zone": "America/Chicago"},
    ]
    assert manage_commands(agent.metadata.schedules()) == {"send_invoices"}


def test_the_servers_zone_comes_from_tz_when_it_names_one(monkeypatch):
    monkeypatch.setenv("TZ", ":UTC")
    assert local_time_zone() == "UTC"
    monkeypatch.setenv("TZ", "Europe/Berlin")
    assert local_time_zone() == "Europe/Berlin"


def test_configured_schedules_need_no_framework():
    config = deployangel.configuration()
    config.update(recurring_jobs={"report": "0 6 * * 1"})
    assert [s["key"] for s in ConfiguredSchedules(config).schedules()] == ["report"]
