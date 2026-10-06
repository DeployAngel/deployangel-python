"""Finds a deployment and polls its verdict document until the requested
point: the 15-minute initial check, a verdict, or the end of watching.
Shared by the deployangel command and the MCP server."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from deployangel.cli.client import NotFound

# Exit codes are the primary signal for agents and CI; they match the Ruby
# gem's deployangel command.
EXIT_CODES = {"verified": 0, "failed": 1, "inconclusive": 2}
TIMED_OUT = 3
NOT_FOUND = 4
INITIAL_OK = 6
INITIAL_WARNINGS = 7
UNTIL_MODES = ("initial", "verdict", "closed")
PENDING_POLL = 15


@dataclass
class Outcome:
    exit_code: int
    document: Optional[dict] = None
    timed_out: bool = False
    not_found: bool = False


def exit_code(document: dict, until_mode: str) -> Optional[int]:
    """None means "keep waiting". Failed always returns immediately."""
    verification = document.get("verification") or {}
    verdict = verification.get("verdict")
    if verdict == "failed":
        return EXIT_CODES["failed"]
    if until_mode == "closed":
        return EXIT_CODES.get(verdict) if verdict and verification.get("state") == "closed" else None
    if until_mode == "initial":
        if verdict:
            return EXIT_CODES.get(verdict)
        check = verification.get("initial_check")
        if check:
            return INITIAL_WARNINGS if check.get("result") == "warnings" else INITIAL_OK
        return None
    return EXIT_CODES.get(verdict) if verdict else None


class Waiter:
    def __init__(self, client, sleeper: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 on_progress: Optional[Callable[[str], None]] = None):
        self.client = client
        self.sleeper = sleeper
        self.clock = clock
        self.on_progress = on_progress

    def wait(self, target: dict, until_mode: str = "verdict", timeout: float = 1800, wait: bool = True) -> Outcome:
        """target: {"deployment_id"} | {"version"} | {"commit"} | {"latest": True}"""
        deadline = self.clock() + timeout
        last_state = None
        while True:
            deployment = self._find(target)
            if deployment is None:
                if not wait or self.clock() >= deadline:
                    return Outcome(exit_code=NOT_FOUND, not_found=True)
                if last_state != "pending_registration":
                    self._progress(f"Waiting for deployment {_describe(target)} to be registered…")
                last_state = "pending_registration"
                self.sleeper(_clamp(min(PENDING_POLL, deadline - self.clock()), 1, PENDING_POLL))
                continue

            document = self.client.verification(deployment["id"])
            state = (document.get("verification") or {}).get("state")
            if state != last_state:
                release = (document.get("deployment") or {})
                self._progress(f"{release.get('version') or release.get('commit')}: {state}")
            last_state = state

            code = exit_code(document, until_mode)
            if code is not None:
                return Outcome(exit_code=code, document=document)
            if not wait:
                return Outcome(exit_code=TIMED_OUT, document=document)
            if self.clock() >= deadline:
                return Outcome(exit_code=TIMED_OUT, document=document, timed_out=True)
            poll = _clamp(int(document.get("poll_after_seconds") or 30), 5, 60)
            self.sleeper(_clamp(min(poll, deadline - self.clock()), 1, 60))

    def _find(self, target: dict) -> Optional[dict]:
        try:
            if target.get("deployment_id"):
                return {"id": target["deployment_id"]}
            if target.get("latest"):
                return self.client.latest_deployment()
            found = self.client.deployments(commit=target.get("commit"), version=target.get("version"), limit=1)
            return found[0] if found else None
        except NotFound:
            return None

    def _progress(self, message: str) -> None:
        if self.on_progress:
            self.on_progress(message)


def _describe(target: dict) -> str:
    return target.get("version") or (target.get("commit") or "")[:12] or target.get("deployment_id") or "latest"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
