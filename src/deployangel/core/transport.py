"""Sends gzipped JSON to DeployAngel with the standard library. Only ever
called from the background thread (or at shutdown), never from a request."""

from __future__ import annotations

import gzip
import json
import platform
import urllib.error
import urllib.request
from typing import Optional

from deployangel.version import VERSION


class Result:
    """outcome is "ok" (2xx), "retry" (network error or 5xx), or "drop" (any
    other status, including 429 rate limiting, which the agent honors by
    pausing)."""

    __slots__ = ("outcome", "status", "retry_after", "body")

    def __init__(self, outcome: str, status: Optional[int] = None, retry_after: Optional[int] = None, body=None):
        self.outcome = outcome
        self.status = status
        self.retry_after = retry_after
        self.body = body

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


class Encoded:
    """A payload already encoded as gzipped JSON. Queued telemetry is kept this
    way: a full minute is large as Python objects but small compressed."""

    __slots__ = ("bytes",)

    def __init__(self, data: bytes):
        self.bytes = data


def encode(body) -> Encoded:
    return Encoded(gzip.compress(json.dumps(body, separators=(",", ":")).encode("utf-8")))


class Transport:
    def __init__(self, config):
        self.config = config

    def post(self, path: str, body, timeout: Optional[float] = None) -> Result:
        """body is a dict, or an Encoded payload sent as is."""
        endpoint = self.config.endpoint.rstrip("/")
        data = (body if isinstance(body, Encoded) else encode(body)).bytes
        request = urllib.request.Request(f"{endpoint}/{path.lstrip('/')}", data=data, method="POST", headers={
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "User-Agent": f"deployangel-python/{VERSION} python/{platform.python_version()}",
        })
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.config.timeout) as response:  # noqa: S310
                return Result("ok", response.status, None, _parse(response.read()))
        except urllib.error.HTTPError as error:
            status = error.code
            if 500 <= status <= 599:
                return Result("retry", status)
            retry_after = _int(error.headers.get("Retry-After")) if status == 429 and error.headers else None
            return Result("drop", status, retry_after)
        except Exception as error:
            self.config.logger.debug("DeployAngel transport error: %s: %s", type(error).__name__, error)
            return Result("retry")


def _parse(body: bytes):
    try:
        return json.loads(body) if body else {}
    except ValueError:
        return {}


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
