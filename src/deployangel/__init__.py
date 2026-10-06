"""DeployAngel agent for Python web apps and job workers.

Framework integrations call start(); the rest of this module is the API they
record through, plus checkpoint() and notify() for application code. Every
function here is safe to call anywhere: it never raises and never touches the
network."""

from __future__ import annotations

import atexit
import os
import re
import sys
import threading
from typing import Optional

from deployangel.agent import Agent
from deployangel.config import Configuration
from deployangel.version import VERSION

__all__ = ["VERSION", "checkpoint", "configuration", "configure", "notify", "recording", "shutdown", "start"]

# Web servers and job workers report from boot, so idle processes still send
# heartbeats; other processes (manage.py commands, shells) only report after
# recording something.
EAGER_PROGRAMS = re.compile(r"\b(gunicorn|uvicorn|hypercorn|daphne|granian|waitress|uwsgi|celery|rqworker|rq)\b")
CHECKPOINT_NAME = re.compile(r"\A[a-z0-9][a-z0-9_.:-]{0,99}\Z", re.IGNORECASE)

_configuration: Optional[Configuration] = None
_agent: Optional[Agent] = None
_lock = threading.Lock()
_capabilities = ["http", "exceptions"]
_metadata_sources: list = []
_hooks_installed = False
_warned_checkpoint = False


def configuration() -> Configuration:
    global _configuration
    if _configuration is None:
        _configuration = Configuration()
    return _configuration


def configure(**options) -> Configuration:
    """deployangel.configure(environments=["production", "staging"])"""
    config = configuration()
    config.update(**options)
    return config


def agent() -> Optional[Agent]:
    return _agent


def start(environment: Optional[str] = None, root: Optional[str] = None, framework: Optional[str] = None,
          framework_version: Optional[str] = None, eager: Optional[bool] = None, debug: bool = False) -> Optional[Agent]:
    """Starts this process's agent, once; later calls return the same agent.
    Framework integrations call it at boot. debug is the framework's debug
    flag, which names the environment "development" unless it's configured."""
    global _agent
    try:
        with _lock:
            if _agent is not None:
                return _agent
            config = configuration()
            environment = config.environment or environment or ("development" if debug else "production")
            _agent = Agent(config=config, environment=environment, root=config.root or root or os.getcwd(),
                           framework=framework, framework_version=framework_version,
                           eager=server_process() if eager is None else eager, capabilities=_capabilities)
            from deployangel.metadata import Metadata

            _agent.metadata = Metadata(config=config, root=_agent.root, environment=environment, sources=_metadata_sources)
            _install_process_hooks()
            return _agent
    except Exception as error:
        try:
            configuration().logger.warning("DeployAngel failed to start: %s: %s", type(error).__name__, error)
        except Exception:
            pass
        return None


def recording() -> bool:
    return _agent is not None and _agent.active


def checkpoint(name: str, count: int = 1) -> None:
    """Counts a business event, such as deployangel.checkpoint("order.fulfilled").
    DeployAngel learns each checkpoint's normal rate and fails a release after
    which it drops sharply or stops."""
    global _warned_checkpoint
    try:
        name = str(name)
        if not CHECKPOINT_NAME.match(name) or not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            if not _warned_checkpoint:
                configuration().logger.warning("DeployAngel ignored checkpoint %r: use letters, numbers, and . _ : - (max 100)", name)
            _warned_checkpoint = True
            return
        add_capability("checkpoints")
        if _agent is not None:
            _agent.record_checkpoint(name, count)
    except Exception:
        pass


def notify(exception: BaseException) -> None:
    """Reports an exception the app caught and handled itself. It's shown for
    context; only unhandled exceptions count toward verdicts."""
    if _agent is not None:
        _agent.record_exception(exception, handled=True)


def record_request(route_key: str, status: int, duration_ms: float, unhandled: bool = False, in_totals: bool = True) -> None:
    if _agent is not None:
        _agent.record_request(route_key, status, duration_ms, unhandled=unhandled, in_totals=in_totals)


def record_job(job_class: str, duration_ms: float, failed: bool = False, discarded: bool = False,
               queue_latency_ms: Optional[float] = None) -> None:
    if _agent is not None:
        _agent.record_job(job_class, duration_ms, failed=failed, discarded=discarded, queue_latency_ms=queue_latency_ms)


def record_exception(exception: BaseException, source: Optional[str] = None, handled: bool = False) -> None:
    if _agent is not None:
        _agent.record_exception(exception, source=source, handled=handled)


def add_capability(name: str) -> None:
    """Signals this process can observe, announced in every payload so the
    cloud never claims to verify what the agent cannot see."""
    if name not in _capabilities:
        _capabilities.append(name)


def add_metadata_source(source) -> None:
    """An integration that knows part of the app: an object with any of
    routes(), job_classes(), job_class_files(), and schedules()."""
    if source not in _metadata_sources:
        _metadata_sources.append(source)


def after_fork() -> None:
    if _agent is not None:
        _agent.after_fork()


def shutdown() -> None:
    if _agent is not None:
        _agent.shutdown()


def server_process() -> bool:
    argv = getattr(sys, "orig_argv", None) or sys.argv
    return bool(EAGER_PROGRAMS.search(" ".join(os.path.basename(str(arg)) if i == 0 else str(arg)
                                                for i, arg in enumerate(argv[:4]))))


def _install_process_hooks() -> None:
    global _hooks_installed
    if _hooks_installed:
        return
    _hooks_installed = True
    atexit.register(shutdown)
    if hasattr(os, "register_at_fork"):
        os.register_at_fork(after_in_child=after_fork)


def _reset_for_tests() -> None:
    global _agent, _configuration, _warned_checkpoint
    _agent = None
    _configuration = None
    _warned_checkpoint = False
    del _capabilities[2:]
    _metadata_sources.clear()
