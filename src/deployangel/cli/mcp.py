"""A Model Context Protocol server over stdio (JSON-RPC 2.0, one message per
line), so coding agents can ask whether their release passed production.
stdout carries only protocol messages. Same tools as the Ruby gem's server.

The tools are read-only with respect to production: nothing here rolls
back, restarts, or changes customer infrastructure."""

from __future__ import annotations

import json
import sys
import time
from typing import Callable, Optional

from deployangel.cli.client import ApiError
from deployangel.cli.waiter import TIMED_OUT, Waiter
from deployangel.version import VERSION

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_WAIT_SECONDS = 300

INSTRUCTIONS = """DeployAngel verifies deployments in production. After deploying, call wait_for_verification
with until "initial" (about 15 minutes) or "verdict". Verdicts: verified means cleared, so you may
report the release as successful. inconclusive means NOT verified: never report it as success.
failed means production regressed: read the findings and exceptions and investigate.
Findings are deterministic evidence; any "investigation" field is AI inference. These tools cannot
change production. Do not roll back or change production without explicit approval.
If a release isn't cleared yet, get_exercise_plan says what to exercise against production so it
clears sooner. Items with needed true are what clearance waits on; the rest are only worth running.
Only act on status "exercisable" or "waiting_for_activity"; for "warm_up" or
"no_baseline" nothing you run can clear it. Exercise routes marked mutating only with a test account
or after asking. Report what you ran with the plan's report_with command (deployangel check).
"""

TARGET_PROPERTIES = {
    "commit": {"type": "string", "description": "Commit SHA (prefix of 7+ characters is fine)."},
    "version": {"type": "string", "description": "Release version, e.g. v184."},
    "deployment_id": {"type": "string", "description": "DeployAngel deployment ID."},
    "latest": {"type": "boolean", "description": "Use the most recent deployment."},
}

ERROR_CODES = {"parse": -32700, "invalid": -32600, "method": -32601, "params": -32602, "internal": -32603}

MEANINGS = {
    0: "verified: the release is cleared",
    1: "failed: production regressed",
    2: "inconclusive: NOT verified; do not report success",
    3: "still in progress; call wait_for_verification again",
    6: "initial check: no problems so far, NOT cleared yet",
    7: "initial check: warnings present, NOT cleared",
}


class Server:
    def __init__(self, client, input=None, output=None, error_output=None, sleeper: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time, git_head: Callable[[], Optional[str]] = lambda: None):
        self.client = client
        self.input = input or sys.stdin
        self.output = output or sys.stdout
        self.error_output = error_output or sys.stderr
        self.sleeper = sleeper
        self.clock = clock
        self.git_head = git_head
        self._scopes = None

    def run(self) -> None:
        for line in self.input:
            if not line.strip():
                continue
            response = self.handle_line(line)
            if response is None:
                continue
            self.output.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.output.flush()

    def handle_line(self, line: str) -> Optional[dict]:
        try:
            message = json.loads(line)
        except ValueError:
            return _error(None, "parse", "invalid JSON")
        if not isinstance(message, dict):
            return _error(None, "invalid", "expected a JSON object")
        return self.handle(message)

    def handle(self, message: dict) -> Optional[dict]:
        id = message.get("id")
        method = message.get("method")
        if id is None:  # notifications, including notifications/initialized
            return None
        params = message.get("params") or {}
        try:
            if method == "initialize":
                return _result(id, self._initialize_result(params.get("protocolVersion")))
            if method == "ping":
                return _result(id, {})
            if method == "tools/list":
                return _result(id, {"tools": self.tools()})
            if method == "tools/call":
                return _result(id, self._call_tool(params.get("name"), params.get("arguments") or {}))
            return _error(id, "method", f"unknown method: {method}")
        except (ValueError, KeyError, TypeError) as error:
            return _error(id, "params", str(error))
        except Exception as error:  # noqa: BLE001 - reported to stderr, never to the agent as a crash
            self.error_output.write(f"deployangel mcp: {type(error).__name__}: {error}\n")
            return _error(id, "internal", "internal error")

    def tools(self) -> list:
        tools = [
            _tool("get_verification", "Current verdict document for a deployment: state, verdict, confidence, coverage, "
                  "findings, new exceptions, clearance report, and what is still watched. Defaults to the current git HEAD.",
                  TARGET_PROPERTIES),
            _tool("wait_for_verification", "Wait for a deployment's verification. until=initial returns at the 15-minute "
                  'initial check ("no problems so far" is NOT clearance); until=verdict waits for verified, failed, or '
                  "inconclusive. Returns the in-progress document if timeout_seconds (max 300) is reached; call again "
                  "while it is still in progress. Inconclusive means not verified.",
                  {**TARGET_PROPERTIES,
                   "until": {"type": "string", "enum": ["initial", "verdict"], "default": "initial"},
                   "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": MAX_WAIT_SECONDS, "default": MAX_WAIT_SECONDS}}),
            _tool("get_exercise_plan", "What stands between a release and clearance, and what to exercise against "
                  "production so it clears sooner: normally active routes short of their runs, routes this release changed "
                  "that haven't run, and critical flows. needed marks what clearance waits on; the rest are only worth running. "
                  "Routes marked mutating change data: use a test account or ask first. "
                  "Your requests count as ordinary traffic; report what you ran with report_with. Defaults to the current git HEAD.",
                  TARGET_PROPERTIES),
            _tool("list_deployments", "Recent deployments with their verification state and verdict.",
                  {"limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}),
            _tool("get_exception", "Sanitized details and the application stack trace for an exception fingerprint.",
                  {"fingerprint": {"type": "string"}}, required=["fingerprint"]),
            _tool("list_late_regressions", "Failures found after a release was cleared, on paths the clearance did not cover.",
                  {"since": {"type": "string", "description": "ISO 8601 time"},
                   "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}}),
        ]
        if "deployments" in self._token_scopes():
            tools.append(_tool("register_deployment", "Register a deployment so it is verified (manual and CI deploys).",
                               {"commit": {"type": "string"}, "version": {"type": "string"},
                                "kind": {"type": "string", "enum": ["code", "config", "rollback", "promotion"]}}))
        return tools

    def _initialize_result(self, requested) -> dict:
        return {
            "protocolVersion": requested if requested in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "deployangel", "version": VERSION},
            "instructions": INSTRUCTIONS,
        }

    def _call_tool(self, name, arguments: dict) -> dict:
        try:
            if name == "get_verification":
                content = self._verification_content(self._waiter().wait(self._target(arguments), wait=False))
            elif name == "wait_for_verification":
                content = self._wait_content(arguments)
            elif name == "get_exercise_plan":
                content = self._plan_content(self._waiter().wait(self._target(arguments), wait=False))
            # MCP requires structured content to be an object, never a bare list.
            elif name == "list_deployments":
                content = {"deployments": self.client.deployments(limit=arguments.get("limit", 10))}
            elif name == "get_exception":
                if "fingerprint" not in arguments:
                    raise ValueError("fingerprint is required")
                content = self.client.exception(arguments["fingerprint"])
            elif name == "list_late_regressions":
                content = {"late_regressions": self.client.late_regressions(since=arguments.get("since"), limit=arguments.get("limit", 10))}
            elif name == "register_deployment":
                if "deployments" not in self._token_scopes():
                    raise ValueError("register_deployment is not available for this token")
                content = self.client.register_deployment(commit=arguments.get("commit"), version=arguments.get("version"),
                                                          kind=arguments.get("kind"))
            else:
                raise ValueError(f"unknown tool: {name}")
        except ApiError as error:
            return {"content": [{"type": "text", "text": f"DeployAngel API error: {error}"}], "isError": True}
        return {"content": [{"type": "text", "text": json.dumps(content, indent=2, ensure_ascii=False)}],
                "structuredContent": content, "isError": False}

    def _wait_content(self, arguments: dict) -> dict:
        until_mode = arguments.get("until", "initial")
        if until_mode not in ("initial", "verdict"):
            raise ValueError("until must be initial or verdict")
        timeout = max(1, min(MAX_WAIT_SECONDS, int(arguments.get("timeout_seconds", MAX_WAIT_SECONDS))))
        return self._verification_content(self._waiter().wait(self._target(arguments), until_mode=until_mode, timeout=timeout))

    def _verification_content(self, outcome) -> dict:
        if outcome.not_found:
            return {"found": False, "note": "No matching deployment is registered yet."}
        return {"exit_code": outcome.exit_code, "meaning": MEANINGS.get(outcome.exit_code, "unknown"),
                "in_progress": outcome.exit_code == TIMED_OUT, "verification": outcome.document}

    def _plan_content(self, outcome) -> dict:
        if outcome.not_found:
            return {"found": False, "note": "No matching deployment is registered yet."}
        plan = outcome.document.get("exercise_plan") or {"note": "This DeployAngel server doesn't return exercise plans yet."}
        return {"deployment": outcome.document.get("deployment"), "exercise_plan": plan}

    def _target(self, arguments: dict) -> dict:
        if arguments.get("deployment_id"):
            return {"deployment_id": str(arguments["deployment_id"])}
        if arguments.get("version"):
            return {"version": str(arguments["version"])}
        if arguments.get("commit"):
            return {"commit": str(arguments["commit"]).lower()}
        if arguments.get("latest"):
            return {"latest": True}
        head = self.git_head()
        return {"commit": head.lower()} if head else {"latest": True}

    def _waiter(self) -> Waiter:
        return Waiter(self.client, sleeper=self.sleeper, clock=self.clock)

    def _token_scopes(self) -> list:
        if self._scopes is None:
            try:
                self._scopes = list(self.client.token_info().get("scopes") or [])
            except ApiError:
                self._scopes = []
        return self._scopes


def _tool(name: str, description: str, properties: dict, required=()) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties, "required": list(required)}}


def _result(id, payload) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": payload}


def _error(id, kind: str, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": ERROR_CODES[kind], "message": message}}
