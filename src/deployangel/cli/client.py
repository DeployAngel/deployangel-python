"""Read and registration API client for the deployangel command and its MCP
server. Unlike the agent's Transport, errors raise, because a person or a
coding agent is waiting on the answer. Standard library only."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

from deployangel.config import DEFAULT_ENDPOINT
from deployangel.version import VERSION


class ApiError(Exception):
    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class NotFound(ApiError):
    pass


class Unauthorized(ApiError):
    pass


class Client:
    def __init__(self, token: Optional[str], endpoint: str = DEFAULT_ENDPOINT, timeout: float = 15.0):
        if not token:
            raise Unauthorized("DEPLOYANGEL_API_TOKEN is not set")
        self.token = token
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    def token_info(self) -> dict:
        return self._get("/api/v1/token")

    def latest_deployment(self) -> dict:
        return self._get("/api/v1/deployments/latest")

    def deployments(self, **filters) -> list:
        return self._get("/api/v1/deployments", filters)["deployments"]

    def exception(self, fingerprint: str) -> dict:
        return self._get(f"/api/v1/exceptions/{urllib.parse.quote(str(fingerprint), safe='')}")

    def late_regressions(self, **filters) -> list:
        return self._get("/api/v1/late_regressions", filters)["late_regressions"]

    def verification(self, deployment_id, all_findings: bool = False) -> dict:
        return self._get(f"/api/v1/deployments/{deployment_id}/verification", {"findings": "all"} if all_findings else {})

    def register_deployment(self, commit=None, version=None, kind=None, provider=None, source_url=None) -> dict:
        body = {"commit": commit, "version": version, "kind": kind, "provider": provider, "source_url": source_url}
        return self._post("/api/v1/deployments", _compact(body))

    def report_check(self, reference: str, name: str, status: str, covers: Iterable[str] = (), details_url=None) -> dict:
        body = {"name": name, "status": status, "covers": list(covers), "details_url": details_url}
        return self._post(f"/api/v1/deployments/{urllib.parse.quote(reference, safe='')}/checks", _compact(body))

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        url = f"{self.endpoint}{path}"
        params = _compact(params or {})
        if params:
            url += "?" + urllib.parse.urlencode(params)
        return self._request(urllib.request.Request(url, method="GET"))

    def _post(self, path: str, body: dict) -> Any:
        request = urllib.request.Request(f"{self.endpoint}{path}", data=json.dumps(body).encode(), method="POST")
        request.add_header("Content-Type", "application/json")
        return self._request(request)

    def _request(self, request: urllib.request.Request) -> Any:
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", f"deployangel-cli-python/{VERSION}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ApiError(f"could not reach {self.endpoint}: {type(error).__name__}: {error}") from error
        return _handle(status, raw)


def _handle(status: int, raw: bytes) -> Any:
    try:
        body = json.loads(raw) if raw.strip() else {}
    except ValueError:
        raise ApiError(f"unexpected response (HTTP {status})", status=status) from None
    message = body.get("error") if isinstance(body, dict) else None
    if 200 <= status < 300:
        return body
    if status == 404:
        raise NotFound(message or "not found", status=404)
    if status in (401, 403):
        raise Unauthorized(message or "unauthorized", status=status)
    raise ApiError(message or f"HTTP {status}", status=status)


def _compact(values: dict) -> dict:
    return {key: value for key, value in values.items() if value is not None}
