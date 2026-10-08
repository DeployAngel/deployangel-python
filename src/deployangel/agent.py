"""The per-process runtime: records requests and jobs into the aggregator and
sends completed minutes from a background thread. Every public method fails
open; nothing here may raise into, or block, the customer's app."""

from __future__ import annotations

import os
import random
import threading
import time
from typing import Callable, Mapping, Optional

from deployangel.core import fingerprint, protocol, redaction, work
from deployangel.core import release as release_module
from deployangel.core.aggregator import Aggregator
from deployangel.core.buffer import Buffer
from deployangel.core.instance import Instance
from deployangel.core.transport import Transport, encode

TELEMETRY_PATH = "/api/v1/telemetry"
METADATA_PATH = "/api/v1/application_metadata"
SHUTDOWN_TIMEOUT = 2.0
DEFAULT_PAUSE = 60
# Seconds past each minute a process waits before sending, chosen once per
# process so the cloud gets a steady stream instead of a burst at the top of
# the minute. The cloud reads a minute 2 minutes after it ends, so a send that
# times out and goes again a minute later still lands in time.
FLUSH_JITTER = (1.0, 50.0)
# Set on an exception object once it's recorded, whichever instrumentation
# sees it first.
SEEN = "__deployangel_recorded__"


class Agent:
    def __init__(self, config, environment: str, root: Optional[str] = None, framework: Optional[str] = None,
                 framework_version: Optional[str] = None, env: Mapping[str, str] = os.environ, transport=None,
                 clock: Callable[[], float] = time.time, eager: bool = False, capabilities: Optional[list] = None):
        self.config = config
        self.active = config.is_active(environment)
        self.environment = environment
        self.root = root
        self.release = release_module.resolve(config, env=env, root=root) if self.active else release_module.Release(None, None, "unknown")
        # A REVISION file or setting that names no commit leaves the release as
        # unknown as nothing at all, so the code fingerprint may name it too.
        if self.release.unknown:
            self.release.pending = True
        self.runtime = protocol.runtime(framework, framework_version)
        self.metadata = None
        self.capabilities = capabilities if capabilities is not None else ["http", "exceptions"]
        self._transport = transport or Transport(config)
        self._env = env
        self._clock = clock
        self._eager = eager
        self._warned: set = set()
        self._reset_process_state()
        if self.active and not self.release.pending:
            self._warn_about_release()
        if self.active and eager:
            self.start_reporter()

    def record_request(self, route_key: str, status: int, duration_ms: float, unhandled: bool = False, in_totals: bool = True) -> None:
        if not self.active:
            return
        try:
            self._check_fork()
            self.start_reporter()
            self._aggregator.record(route_key, status, duration_ms, unhandled=unhandled, in_totals=in_totals)
        except Exception as error:
            self._warn_once("record", f"DeployAngel failed to record a request: {type(error).__name__}: {error}")

    def record_job(self, job_class: str, duration_ms: float, failed: bool = False, discarded: bool = False,
                   queue_latency_ms: Optional[float] = None) -> None:
        if not self.active:
            return
        try:
            self._check_fork()
            self.start_reporter()
            self._aggregator.record_job(job_class, duration_ms, failed=failed, discarded=discarded, queue_latency_ms=queue_latency_ms)
        except Exception as error:
            self._warn_once("record_job", f"DeployAngel failed to record a job: {type(error).__name__}: {error}")

    def record_discard(self, job_class: str) -> None:
        if not self.active:
            return
        try:
            self._check_fork()
            self._aggregator.record_discard(job_class)
        except Exception as error:
            self._warn_once("record_discard", f"DeployAngel failed to record a discarded job: {type(error).__name__}: {error}")

    def record_checkpoint(self, name: str, count: int = 1) -> None:
        if not self.active:
            return
        try:
            self._check_fork()
            self.start_reporter()
            self._aggregator.record_checkpoint(name, count, work.current())
        except Exception as error:
            self._warn_once("record_checkpoint", f"DeployAngel failed to record a checkpoint: {type(error).__name__}: {error}")

    def record_exception(self, exception: BaseException, source: Optional[str] = None, handled: bool = False) -> None:
        """Records each exception object once, whichever instrumentation sees it
        first."""
        if not self.active or not isinstance(exception, BaseException):
            return
        try:
            if getattr(exception, SEEN, False):
                return
            try:
                setattr(exception, SEEN, True)
            except Exception:
                pass
            messages = self.config.exception_messages
            details = fingerprint.for_exception(exception, self.root, message=messages,
                                                redactions=redaction.current() if messages else ())
            self.record_exception_details(details, fingerprint.backtrace(exception, self.root), source=source, handled=handled)
        except Exception as error:
            self._warn_once("record_exception", f"DeployAngel failed to record an exception: {type(error).__name__}: {error}")

    def record_exception_details(self, details: dict, backtrace: Optional[list], source: Optional[str] = None,
                                 handled: bool = False) -> None:
        """An exception already fingerprinted, such as one parsed from a
        formatted traceback."""
        if not self.active:
            return
        try:
            self._check_fork()
            self.start_reporter()
            self._aggregator.record_exception(details, source=source, handled=handled, backtrace=backtrace)
        except Exception as error:
            self._warn_once("record_exception", f"DeployAngel failed to record an exception: {type(error).__name__}: {error}")

    def flush(self, include_current: bool = False, deadline: Optional[float] = None) -> int:
        """Drains completed minutes into the buffer, encoded so unsent minutes
        stay small while DeployAngel is unreachable, and sends what it can."""
        if not self.active:
            return 0
        try:
            periods = self._aggregator.drain(include_current=include_current, max_periods=self.config.max_queued_payloads)
            release = self._resolved_release() if periods else None
            for period in periods:
                self._buffer.push(encode(protocol.telemetry(period, self.instance, release, self.runtime, self.capabilities)))
            return self._send_buffered(deadline)
        except Exception as error:
            self._warn_once("flush", f"DeployAngel failed to flush telemetry: {type(error).__name__}: {error}")
            return 0

    def start_reporter(self) -> None:
        if not self.active or (self._thread is not None and self._thread.is_alive()):
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopping.clear()
            thread = threading.Thread(target=self._run_reporter, name="deployangel-reporter", daemon=True)
            thread.start()
            self._thread = thread

    def shutdown(self, timeout: float = SHUTDOWN_TIMEOUT) -> None:
        """Sends the in-progress minute too, bounded by a short timeout, because
        deployments restart processes. Runs on the calling thread: new threads
        can't start while the interpreter shuts down."""
        if not self.active:
            return
        try:
            if os.getpid() != self._pid:
                return
            self._stopping.set()
            self.flush(include_current=True, deadline=time.monotonic() + timeout)
        except Exception:
            pass

    def after_fork(self) -> None:
        """Threads do not survive fork, and the parent's identity and counts
        must not be reused by the child."""
        self._reset_process_state()
        if self.active and self._eager:
            self.start_reporter()

    def send_metadata(self) -> None:
        """Sent once per process; retried on the next minute if it fails. File
        digests are only uploaded when the cloud has not seen the manifest."""
        if self._metadata_sent or self.metadata is None or self._paused():
            return
        try:
            base = {"protocol_version": protocol.VERSION, "instance": self.instance.to_protocol(),
                    "release": self._resolved_release().to_protocol(), "runtime": self.runtime}
            base.update(self.metadata.to_protocol())
            result = self._transport.post(METADATA_PATH, base)
            if not result.ok:
                return
            if isinstance(result.body, dict) and result.body.get("manifest_needed"):
                if not self._transport.post(METADATA_PATH, dict(base, files=self.metadata.files())).ok:
                    return
            self._metadata_sent = True
        except Exception as error:
            self._warn_once("metadata", f"DeployAngel failed to send application metadata: {type(error).__name__}: {error}")

    def _resolved_release(self):
        """The release for a payload. When nothing named it at boot, the code
        fingerprint is computed here, once: from the reporter thread before its
        first send, or the final flush at shutdown, never in a request or job.
        It's the hash of the file digests the metadata sends, so the manifest
        is built once for both."""
        if not self.release.pending:
            return self.release
        with self._release_lock:
            if self.release.pending:
                manifest = None
                try:
                    if self.metadata is not None:
                        manifest = self.metadata.file_manifest()
                except Exception:
                    manifest = None
                self.release = release_module.code_fingerprint(manifest)
                self._warn_about_release()
        return self.release

    def _warn_about_release(self) -> None:
        if self.release.unknown:
            self._warn_once("unknown_release", "DeployAngel could not determine the release; set DEPLOYANGEL_REVISION "
                            "or enable Heroku dyno metadata. Telemetry will not be attributed to deployments.")
        elif self.release.source == "code_fingerprint":
            self._warn_once("code_fingerprint", "DeployAngel identifies releases by a fingerprint of the app's code. Set "
                            "DEPLOYANGEL_REVISION to the deployed commit to see each release's commits and pull requests.")

    def _check_fork(self) -> None:
        # A fork the interpreter didn't see, such as uWSGI's, which forks
        # workers from C.
        if os.getpid() != self._pid:
            self.after_fork()

    def _reset_process_state(self) -> None:
        self._pid = os.getpid()
        self.instance = Instance(env=self._env, now=self._clock())
        self._aggregator = Aggregator(max_routes=self.config.max_routes, clock=self._clock)
        self._buffer = Buffer(self.config.max_queued_payloads)
        self._paused_until: Optional[float] = None
        self._metadata_sent = False
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()
        # New in a child: one the parent's reporter held mid-fingerprint would
        # never be released there.
        self._release_lock = threading.Lock()
        self._stopping = threading.Event()
        self._jitter = random.uniform(*FLUSH_JITTER)

    def _run_reporter(self) -> None:
        try:
            # Off the request path, so a process that exits within a minute or
            # two doesn't build the fingerprint at shutdown instead. Only once
            # the framework has attached the metadata it's built from.
            if self.metadata is not None:
                self._resolved_release()
            while not self._stopping.is_set():
                if self._stopping.wait(self._seconds_until_next_flush()):
                    break
                self.send_metadata()
                self.flush()
        except Exception as error:
            self._warn_once("reporter", f"DeployAngel reporter stopped: {type(error).__name__}: {error}")

    def _seconds_until_next_flush(self) -> float:
        interval = self.config.flush_interval
        return (interval - (self._clock() % interval)) + self._jitter

    def _send_buffered(self, deadline: Optional[float] = None) -> int:
        sent = 0
        while True:
            payload = self._buffer.shift()
            if payload is None:
                break
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._buffer.unshift(payload)
                    break
            if self._paused():
                self._buffer.unshift(payload)
                break
            result = self._transport.post(TELEMETRY_PATH, payload, timeout=min(remaining, self.config.timeout) if remaining else None)
            if result.outcome == "ok":
                sent += 1
            elif result.outcome == "retry":
                self._buffer.unshift(payload)
                break
            else:
                self._handle_rejection(result)
        return sent

    def _handle_rejection(self, result) -> None:
        if result.status == 429:
            self._paused_until = self._clock() + (result.retry_after if result.retry_after and result.retry_after > 0 else DEFAULT_PAUSE)
        elif result.status in (401, 403):
            self._warn_once("auth", f"DeployAngel rejected the token (HTTP {result.status}); check DEPLOYANGEL_TOKEN "
                            "and that it has the telemetry scope.")
        else:
            self._warn_once(f"rejected_{result.status}", f"DeployAngel rejected a telemetry payload (HTTP {result.status}).")

    def _paused(self) -> bool:
        return self._paused_until is not None and self._clock() < self._paused_until

    def _warn_once(self, key: str, message: str) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        try:
            self.config.logger.warning(message)
        except Exception:
            pass
