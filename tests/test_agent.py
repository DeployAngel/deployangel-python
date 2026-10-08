import gzip
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import FakeTransport, drain

import deployangel
from deployangel import http
from deployangel.agent import Agent
from deployangel.config import Configuration
from deployangel.core.transport import Result, Transport


def make_agent(fake_clock, transport=None, **options):
    config = Configuration(env={})
    config.update(token="da_test", revision="abc1234", file_digests=False, **options)
    return Agent(config=config, environment="production", root="/app", framework="django", framework_version="5.2",
                 env={"DYNO": "web.1"}, transport=transport or FakeTransport(), clock=fake_clock)


def test_sends_completed_minutes_as_protocol_v1(fake_clock):
    agent = make_agent(fake_clock)
    agent.record_request("GET /products/", 200, 84)
    agent.record_request("POST /orders/", 500, 120, unhandled=True)
    fake_clock.advance(60)
    assert agent.flush() == 1

    (payload,) = agent._transport.telemetry()
    assert payload["protocol_version"] == 1
    assert payload["agent"]["name"] == "deployangel-python"
    assert payload["runtime"]["language"] == "python"
    assert payload["runtime"]["framework"] == "django"
    assert payload["release"] == {"version": None, "commit": "abc1234", "source": "config"}
    assert payload["instance"]["host"] == "web.1" and payload["instance"]["process_type"] == "web"
    assert payload["period"] == {"started_at": "2026-09-30T14:00:00Z", "duration_seconds": 60}
    assert payload["http"]["requests"] == 2
    assert payload["http"]["status_counts"] == {"500": 1}
    assert payload["http"]["unhandled_exceptions"] == 1
    assert payload["http"]["latency_histogram"]["scheme"] == "log1.1_ms_v1"
    assert [route["key"] for route in payload["routes"]] == ["GET /products/", "POST /orders/"]
    assert payload["capabilities"] == ["http", "exceptions"]


def test_does_nothing_without_a_token(fake_clock):
    config = Configuration(env={})
    agent = Agent(config=config, environment="production", transport=FakeTransport(), clock=fake_clock)
    agent.record_request("GET /", 200, 1)
    assert agent.flush(include_current=True) == 0
    assert agent._transport.posts == []


def test_keeps_payloads_for_later_when_the_cloud_is_unreachable(fake_clock):
    transport = FakeTransport(Result("retry"), Result("ok", 202))
    agent = make_agent(fake_clock, transport)
    agent.record_request("GET /", 200, 1)
    fake_clock.advance(60)
    assert agent.flush() == 0
    fake_clock.advance(60)
    assert agent.flush() == 2
    assert len(transport.posts) == 3


def test_pauses_after_rate_limiting(fake_clock):
    transport = FakeTransport(Result("drop", 429, 120))
    agent = make_agent(fake_clock, transport)
    fake_clock.advance(60)
    agent.flush()
    fake_clock.advance(60)
    assert agent.flush() == 0
    assert len(transport.posts) == 1
    fake_clock.advance(120)
    assert agent.flush() == 3


def test_records_an_exception_once_with_its_source(started_agent):
    agent = started_agent()
    try:
        raise ValueError("order 42 not found")
    except ValueError as error:
        deployangel.record_exception(error, source="route:GET /orders/<int:pk>/")
        deployangel.record_exception(error, source="job_class:other")
    (entry,) = drain(agent)["exceptions"]
    assert entry["exception_class"] == "ValueError"
    assert entry["message"] == "order <n> not found"
    assert entry["count"] == 1
    assert entry["sources"] == {"route:GET /orders/<int:pk>/": 1}
    assert entry["top_frame"] == "tests/test_agent.py#test_records_an_exception_once_with_its_source"
    assert entry["app_frame"] is True
    assert entry["backtrace"] == ["tests/test_agent.py#test_records_an_exception_once_with_its_source"]


def test_leaves_messages_out_when_they_are_turned_off(started_agent):
    agent = started_agent(exception_messages=False)
    try:
        raise ValueError("patient Jane Doe")
    except ValueError as error:
        deployangel.notify(error)
    (entry,) = drain(agent)["exceptions"]
    assert "message" not in entry
    assert (entry["count"], entry["handled_count"]) == (0, 1)


def test_checkpoints_are_counted_and_announced(started_agent):
    agent = started_agent()
    deployangel.checkpoint("order.created")
    deployangel.checkpoint("order.created", count=2)
    deployangel.checkpoint("not a valid name!")
    payload = drain(agent)
    # Outside any request or job: neither, but both fields are always sent.
    assert payload["checkpoints"] == [{"key": "order.created", "count": 3, "http": 0, "job": 0}]
    assert "checkpoints" in payload["capabilities"]


def test_checkpoints_report_whether_they_were_recorded_in_a_request_or_a_job(started_agent):
    from deployangel.core import work

    agent = started_agent()
    request = work.begin(work.HTTP)
    deployangel.checkpoint("order.created", count=3)
    job = work.begin(work.JOB)  # a task run eagerly inside the request
    deployangel.checkpoint("order.created")
    work.end(job)
    deployangel.checkpoint("order.created")
    work.end(request)
    deployangel.checkpoint("order.created", count=2)
    deployangel.checkpoint("export.finished")
    payload = drain(agent)
    assert payload["checkpoints"] == [{"key": "order.created", "count": 7, "http": 4, "job": 1},
                                      {"key": "export.finished", "count": 1, "http": 0, "job": 0}]


def test_sends_metadata_once_and_files_only_when_the_cloud_asks(fake_clock):
    transport = FakeTransport(Result("ok", 200, None, {"manifest_needed": True}), Result("ok", 200, None, {}))
    agent = make_agent(fake_clock, transport)

    class Source:
        def routes(self):
            return [{"key": "GET /x/", "controller": "shop.views", "action": "x"}]

        def job_classes(self):
            raise RuntimeError("a broken source doesn't stop the others")

    from deployangel.metadata import Metadata

    agent.metadata = Metadata(agent.config, "/app", "production", sources=[Source()])
    agent.send_metadata()
    agent.send_metadata()
    first, second = transport.posts
    assert first[0] == "/api/v1/application_metadata"
    assert first[1]["routes"] == [{"key": "GET /x/", "controller": "shop.views", "action": "x"}]
    assert first[1]["job_classes"] == []
    assert "files" not in first[1] and second[1]["files"] == {}


class Warnings:
    def __init__(self):
        self.messages = []

    def warning(self, message, *args):
        self.messages.append(message % args if args else message)


def fingerprinted_agent(fake_clock, root, **options):
    """An agent nothing names the release for, with metadata that counts how
    often it builds the file manifest."""
    from deployangel.metadata import Metadata

    class CountingMetadata(Metadata):
        builds = 0
        threads: list = []

        def _build_manifest(self):
            CountingMetadata.builds += 1
            CountingMetadata.threads.append(threading.current_thread().name)
            return super()._build_manifest()

    config = Configuration(env={})
    config.update(token="da_test", logger=Warnings(), **options)
    agent = Agent(config=config, environment="production", root=str(root), env={}, transport=FakeTransport(), clock=fake_clock)
    agent.metadata = CountingMetadata(config, str(root), "production")
    return agent, CountingMetadata


def test_fingerprints_the_code_before_the_first_send_not_at_boot(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, metadata = fingerprinted_agent(fake_clock, tmp_path)
    assert agent.release.pending and metadata.builds == 0
    # The first request starts the reporter, which fingerprints the code in
    # the background; the request itself never does.
    agent.record_request("GET /", 200, 1)
    assert threading.current_thread().name not in metadata.threads

    agent.send_metadata()
    fake_clock.advance(60)
    agent.flush()
    expected = {"version": "code:" + agent.metadata.file_manifest()["hash"][:12], "commit": None, "source": "code_fingerprint"}
    (_, metadata_payload), (_, telemetry) = agent._transport.posts
    assert metadata_payload["release"] == expected and telemetry["release"] == expected
    fake_clock.advance(60)
    agent.flush()
    assert metadata.builds == 1
    assert agent.config.logger.messages == [
        "DeployAngel identifies releases by a fingerprint of the app's code. Set DEPLOYANGEL_REVISION to the "
        "deployed commit to see each release's commits and pull requests."]


def test_the_reporter_fingerprints_the_code_as_soon_as_it_starts(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, metadata = fingerprinted_agent(fake_clock, tmp_path)
    agent.start_reporter()
    try:
        deadline = time.monotonic() + 2
        while agent.release.pending and time.monotonic() < deadline:
            time.sleep(0.01)
        assert metadata.threads == ["deployangel-reporter"]
        assert agent.release.source == "code_fingerprint"
    finally:
        agent._stopping.set()


def test_the_reporter_waits_for_the_metadata_when_it_starts_first(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, metadata = fingerprinted_agent(fake_clock, tmp_path)
    attached, agent.metadata = agent.metadata, None
    agent.start_reporter()
    try:
        time.sleep(0.05)
        assert agent.release.pending
        agent.metadata = attached
        agent.record_request("GET /", 200, 1)
        fake_clock.advance(60)
        agent.flush()
        assert agent._transport.posts[-1][1]["release"]["source"] == "code_fingerprint"
    finally:
        agent._stopping.set()


def test_a_revision_file_that_names_no_commit_falls_back_to_the_fingerprint(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    (tmp_path / "REVISION").write_text("\n")
    agent, _ = fingerprinted_agent(fake_clock, tmp_path)
    assert agent._resolved_release().source == "code_fingerprint"


def test_the_fingerprint_is_computed_once_across_threads(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, metadata = fingerprinted_agent(fake_clock, tmp_path)
    threads = [threading.Thread(target=agent._resolved_release) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert metadata.builds == 1 and agent.release.source == "code_fingerprint"


def test_without_file_digests_the_release_is_unknown_and_says_so_once_resolved(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, _ = fingerprinted_agent(fake_clock, tmp_path, file_digests=False)
    assert agent.config.logger.messages == []
    fake_clock.advance(60)
    agent.flush()
    assert agent._transport.telemetry()[0]["release"] == {"version": None, "commit": None, "source": "unknown"}
    assert len(agent.config.logger.messages) == 1 and "could not determine the release" in agent.config.logger.messages[0]


def test_without_metadata_the_release_is_unknown(fake_clock, tmp_path):
    agent, _ = fingerprinted_agent(fake_clock, tmp_path)
    agent.metadata = None
    agent.shutdown()
    assert agent._transport.telemetry()[0]["release"]["source"] == "unknown"


def test_the_shutdown_flush_fingerprints_a_process_that_never_sent(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, _ = fingerprinted_agent(fake_clock, tmp_path)
    agent.record_request("GET /", 200, 1)
    agent.shutdown()
    assert agent._transport.telemetry()[0]["release"]["source"] == "code_fingerprint"


def test_a_forked_child_never_waits_on_the_parents_fingerprint_lock(fake_clock, tmp_path):
    (tmp_path / "app.py").write_text("print('hi')\n")
    agent, _ = fingerprinted_agent(fake_clock, tmp_path)
    agent._release_lock.acquire()  # held by the parent's reporter when it forked
    agent.after_fork()
    assert agent._resolved_release().source == "code_fingerprint"


def test_a_named_release_skips_the_fingerprint(fake_clock, tmp_path):
    agent, metadata = fingerprinted_agent(fake_clock, tmp_path, revision="abc1234")
    assert not agent.release.pending
    fake_clock.advance(60)
    agent.flush()
    assert metadata.builds == 0 and agent._transport.telemetry()[0]["release"]["source"] == "config"


def test_shutdown_sends_the_minute_in_progress(fake_clock):
    agent = make_agent(fake_clock)
    agent.record_request("GET /", 200, 1)
    agent.shutdown()
    (payload,) = agent._transport.telemetry()
    assert payload["http"]["requests"] == 1


def test_a_forked_child_gets_its_own_identity_and_counts(started_agent):
    agent = started_agent()
    deployangel.record_request("GET /parent/", 200, 1)
    parent_id = agent.instance.id
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:
        # Child: os.register_at_fork already reset the agent.
        result = {"id": agent.instance.id, "routes": list(agent._aggregator._periods and
                                                           next(iter(agent._aggregator._periods.values())).routes)}
        os.write(write, json.dumps(result).encode())
        os._exit(0)
    os.close(write)
    os.waitpid(pid, 0)
    child = json.loads(os.read(read, 10000))
    assert child["id"] != parent_id
    assert child["routes"] == []
    assert [route["key"] for route in drain(agent)["routes"]] == ["GET /parent/"]


def test_starts_once_and_names_the_environment(monkeypatch):
    deployangel.configure(token="da_test")
    agent = deployangel.start(framework="flask", debug=True, eager=False)
    assert deployangel.start(framework="other") is agent
    assert agent.environment == "development" and not agent.active
    assert agent.runtime["framework"] == "flask"


def test_server_processes_report_from_boot(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "orig_argv", ["/app/.heroku/python/bin/python", "/app/.heroku/python/bin/gunicorn", "shop.wsgi"])
    assert deployangel.server_process()
    monkeypatch.setattr(sys, "orig_argv", ["python", "manage.py", "migrate"])
    assert not deployangel.server_process()
    monkeypatch.setattr(sys, "orig_argv", ["/usr/bin/python3", "-m", "celery", "-A", "shop", "worker"])
    assert deployangel.server_process()


@pytest.mark.parametrize("pattern,skipped", [
    ("/healthz", True), ("/api/livez/", True), ("/health/", True), ("/patients/<int:pk>/health/", False),
    ("/items/{item_id}/ping", False), ("/products/", False), (None, False),
])
def test_health_checks_are_recognized_by_path(pattern, skipped):
    assert http.is_health_check_path(pattern) is skipped


def test_http_records_unmatched_errors_outside_the_totals(started_agent):
    agent = started_agent(ignored_routes=["GET /status/"])
    started = time.perf_counter()
    http.record("GET", None, 200, started)  # a static file
    http.record("GET", None, 404, started)  # a bot probing /wp-admin
    http.record("GET", "/status/", 200, started)
    http.record("GET", "/healthz/", 200, started)
    http.record("GET", "/products/", 200, started)
    payload = drain(agent)
    assert payload["http"]["requests"] == 1
    assert {route["key"]: route["requests"] for route in payload["routes"]} == {"GET unmatched": 1, "GET /products/": 1}


class _Handler(BaseHTTPRequestHandler):
    responses = []
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        _Handler.received.append((self.path, dict(self.headers), json.loads(gzip.decompress(body))))
        status, headers, payload = _Handler.responses.pop(0)
        if status == "sleep":
            time.sleep(1)
            status = 200
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _Handler.responses.clear()
    _Handler.received.clear()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


def transport_for(endpoint, timeout=5.0):
    config = Configuration(env={})
    config.update(token="da_test", endpoint=endpoint, timeout=timeout)
    return Transport(config)


def test_transport_posts_gzipped_json_with_the_token(server):
    _Handler.responses.append((202, {}, b'{"manifest_needed": true}'))
    result = transport_for(server + "/").post("/api/v1/telemetry", {"a": 1})
    assert (result.outcome, result.status, result.body) == ("ok", 202, {"manifest_needed": True})
    path, headers, payload = _Handler.received[0]
    assert path == "/api/v1/telemetry" and payload == {"a": 1}
    assert headers["Authorization"] == "Bearer da_test"
    assert headers["Content-Encoding"] == "gzip"
    assert headers["User-Agent"].startswith("deployangel-python/")


def test_transport_classifies_failures(server):
    _Handler.responses.extend([(503, {}, b""), (429, {"Retry-After": "30"}, b""), (401, {}, b""), ("sleep", {}, b"")])
    transport = transport_for(server, timeout=0.3)
    assert transport.post("/x", {}).outcome == "retry"
    limited = transport.post("/x", {})
    assert (limited.outcome, limited.status, limited.retry_after) == ("drop", 429, 30)
    assert transport.post("/x", {}).outcome == "drop"
    assert transport.post("/x", {}).outcome == "retry"  # timed out


def test_transport_retries_when_the_cloud_is_down():
    assert transport_for("http://127.0.0.1:9", timeout=0.5).post("/x", {}).outcome == "retry"
