"""Values that name a customer of the app rather than its code, replaced in
exception messages before they're cleaned: the request's host. Kept per
context (thread or asyncio task), where the request that raised ran."""

from __future__ import annotations

import contextvars
import re

_host: contextvars.ContextVar = contextvars.ContextVar("deployangel_request_host", default=None)
# Shorter values would replace ordinary words.
MIN_LENGTH = 3
_PORT = re.compile(r":\d+\Z")


def set_request_host(host) -> None:
    _host.set(host)


def current() -> list:
    """[value, placeholder] pairs, longest first."""
    host = _PORT.sub("", str(_host.get() or ""))
    return [(host, "<host>")] if len(host) >= MIN_LENGTH else []
