"""The unit of work the current code runs in: an HTTP request, a job, or
neither. Integrations mark where they handle a request or run a job, so each
checkpoint can say where it was recorded and the cloud compares it against
the right traffic.

A context variable follows threads and asyncio tasks separately. Nested units
(a task run eagerly inside a request) stack, and the innermost wins; ending
one restores the one around it."""

from __future__ import annotations

import contextvars
from typing import Optional

HTTP = "http"
JOB = "job"

_current: contextvars.ContextVar = contextvars.ContextVar("deployangel_unit_of_work", default=None)


def begin(kind: str) -> Optional[contextvars.Token]:
    """Marks what follows, in this thread or task, as kind (HTTP or JOB) until
    end() is called with the token returned. Call end() in a finally block."""
    try:
        return _current.set(kind)
    except Exception:
        return None


def end(token: Optional[contextvars.Token]) -> None:
    if token is None:
        return
    try:
        _current.reset(token)
    except Exception:
        # A token from another context or one already used: leave the
        # context as it is rather than raise into the app.
        pass


# HTTP, JOB, or None: the innermost unit of work running here. The bound
# method itself, since every checkpoint calls it.
current = _current.get
