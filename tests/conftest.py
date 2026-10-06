import os
import time

import pytest

import deployangel
from deployangel.core.transport import Result

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class FakeClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTransport:
    """Records posts and answers from a queue of results (ok by default)."""

    def __init__(self, *results):
        self.posts = []
        self.results = list(results)

    def post(self, path, body, timeout=None):
        import gzip
        import json

        from deployangel.core.transport import Encoded

        payload = json.loads(gzip.decompress(body.bytes)) if isinstance(body, Encoded) else body
        self.posts.append((path, payload))
        return self.results.pop(0) if self.results else Result("ok", 202, None, {})

    def telemetry(self):
        return [payload for path, payload in self.posts if path.endswith("/telemetry")]


@pytest.fixture
def fake_clock():
    return FakeClock(1790776810.0)  # 2026-09-30 14:00:10 UTC


@pytest.fixture(autouse=True)
def reset_deployangel(monkeypatch):
    for name in list(os.environ):
        if name.startswith(("DEPLOYANGEL_", "HEROKU_", "DYNO")):
            monkeypatch.delenv(name, raising=False)
    deployangel._reset_for_tests()
    yield
    deployangel._reset_for_tests()


@pytest.fixture
def started_agent(monkeypatch):
    """A running agent with a fake transport and no background thread, rooted
    at this repository so test files count as application code."""

    def start(framework="test", **config):
        deployangel.configure(token="da_test", enabled=True, revision="abc1234", root=ROOT, file_digests=False, **config)
        agent = deployangel.start(framework=framework, eager=False)
        agent._transport = FakeTransport()
        monkeypatch.setattr(agent, "start_reporter", lambda: None)
        # Frozen, so a test that crosses a minute still records one period.
        clock = FakeClock(time.time())
        agent._clock = agent._aggregator._clock = clock
        return agent

    return start


def drain(agent):
    """The minute in progress, as the payload the agent would send."""
    agent.flush(include_current=True)
    payloads = agent._transport.telemetry()
    return payloads[-1] if payloads else None
