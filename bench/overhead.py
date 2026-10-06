"""Time and memory the agent adds: recording one request, and a minute with
every list at its cap. Run with: python bench/overhead.py"""

import time
import tracemalloc

import deployangel
from deployangel import http
from deployangel.core import fingerprint
from deployangel.core.transport import Result


class Unreachable:
    """Every send fails, so minutes stay buffered and nothing leaves the machine."""

    def post(self, path, body, timeout=None):
        return Result("retry")


deployangel.configure(token="bench", enabled=True, revision="abc1234", file_digests=False)
agent = deployangel.start(framework="bench", eager=False)
agent.start_reporter = lambda: None
agent._transport = Unreachable()

N = 200_000
started = time.perf_counter()
for i in range(N):
    http.record("GET", "/products/<int:pk>/", 200, time.perf_counter())
per_request = (time.perf_counter() - started) / N * 1e6
print(f"recording a request: {per_request:.2f} µs")

started = time.perf_counter()
for i in range(N):
    deployangel.record_job("shop.tasks.sync", 12.5, queue_latency_ms=40)
print(f"recording a job: {(time.perf_counter() - started) / N * 1e6:.2f} µs")

try:
    raise ValueError("order 42 missing")
except ValueError as error:
    exc = error
started = time.perf_counter()
for i in range(10_000):
    fingerprint.for_exception(exc, "/app")
print(f"fingerprinting an exception: {(time.perf_counter() - started) / 10_000 * 1e6:.1f} µs")

tracemalloc.start()
for minute in range(10):
    agent._clock = agent._aggregator._clock = (lambda m=minute: 1790776800 + m * 60)
    for r in range(150):
        http.record("GET", f"/r{r}/", 500, time.perf_counter())
    for j in range(150):
        deployangel.record_job(f"job{j}", 5)
    for c in range(120):
        deployangel.checkpoint(f"c{c}")
    for e in range(30):
        try:
            raise type(f"E{e}", (Exception,), {})("x")
        except Exception as error:
            deployangel.record_exception(error)
    agent.flush(include_current=True)
current, peak = tracemalloc.get_traced_memory()
print(f"memory with every list at its cap and 10 unsent minutes: {current / 1e6:.2f} MB (peak {peak / 1e6:.2f} MB)")
