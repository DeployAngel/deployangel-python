"""The `deployangel` command for developers, CI, and coding agents: register
deploys, wait for verdicts, say what to exercise, report checks, and run an
MCP server. It talks only to DeployAngel's API, never to the app, and never
starts the agent. Behaves like the Ruby gem's command: same options, output,
and exit codes."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from typing import Callable, Mapping, Optional

from deployangel.cli import ci, credentials, formatter
from deployangel.cli.client import ApiError, Client, NotFound, Unauthorized
from deployangel.cli.waiter import NOT_FOUND, UNTIL_MODES, Waiter
from deployangel.config import DEFAULT_ENDPOINT
from deployangel.version import VERSION

USAGE_ERROR = 5

HELP = f"""Usage: deployangel <command> [options]

  release    Register a deployment          [--commit=SHA] [--version=V] [--kind=code]
                                            [--provider=P] [--url=RUN_URL]
  verify     Report or wait for a verdict   [--commit=SHA | --version=V | --deployment=ID]
                                            [--wait] [--until=initial|verdict|closed] [--timeout=30m]
                                            [--format=text|json] [--all-findings]
  status     Latest deployment and its verification
  plan       What to exercise so a release  [--commit=SHA | --version=V | --deployment=ID]
             clears sooner                  [--format=text|json]
  exercise   Send the plan's read-only      --url=PRODUCTION_URL [--commit=SHA | --version=V | --deployment=ID]
             requests to production         [--max-requests=200] [--dry-run]
  exception  Details for a fingerprint      deployangel exception FINGERPRINT
  check      Report a smoke test result     --name=NAME --status=pass|fail [--covers=a,b]
                                            [--commit=SHA | --deployment=ID] [--details-url=URL]
  install    Add a Kamal deploy hook,       deployangel install kamal
             bake the commit into an image, deployangel install docker
             or set up coding agents        deployangel install agents
  doctor     Check the setup: this checkout  [--format=text|json]
             and what DeployAngel has seen
  setup      Redeem a one-time setup code   deployangel setup --code=CODE
             and save the app's tokens here, without printing them
  token      Print a saved token, to pipe   deployangel token app|api
             into a host's or CI's secrets
  mcp        Run the MCP server over stdio (for coding agents)

With no target, release, verify, plan, and check use the current git HEAD commit.
--format defaults to text in a terminal and json otherwise, such as in a pipe or an agent.
In GitHub Actions, GitLab CI, CircleCI, and Buildkite, release fills in the
commit, a build label (e.g. run-123), and a link to the run automatically.
In a Kamal hook, it registers Kamal's release (KAMAL_VERSION).
Token: DEPLOYANGEL_API_TOKEN, else the one `deployangel setup` saved in .deployangel/.
Environment: DEPLOYANGEL_URL (default {DEFAULT_ENDPOINT})

Exit codes: 0 verified, 1 failed, 2 inconclusive, 3 still in progress / timed out,
            4 deployment not found, 5 usage, auth, or network error,
            6 initial check: no problems so far (NOT cleared), 7 initial check: warnings (NOT cleared)"""

# Registers each Kamal deploy. It runs where `kamal deploy` runs, and never
# fails a deploy that has already shipped.
KAMAL_HOOK = """#!/bin/sh
# Registers each deploy with DeployAngel, which then verifies it in
# production. Needs a "CI deploys" token in DEPLOYANGEL_API_TOKEN where
# you run `kamal deploy`. Added by `deployangel install kamal`.
deployangel release || true
"""
KAMAL_HOOK_LINE = "deployangel release || true"

# The block `deployangel install agents` keeps in AGENTS.md (and CLAUDE.md
# when it doesn't import AGENTS.md), replaced whole on each run.
AGENTS_START = "<!-- deployangel:start -->"
AGENTS_END = "<!-- deployangel:end -->"


def agent_instructions(command: str) -> str:
    return f"""{AGENTS_START}
## Production verification (DeployAngel)

After deploying, or pushing a commit that deploys, wait for DeployAngel's
verdict: call the `wait_for_verification` MCP tool with the commit and
`until: "initial"`, calling again while it's in progress, or run
`{command} verify --commit=<sha> --wait --until=initial`.

- Exit 0 / verified: the release is cleared. Report the clearance line, what
  it says it doesn't cover ("Not assessed", "Not run since the deploy"), and
  anything DeployAngel is still watching, then move on.
- Exit 6: no problems so far, but NOT cleared. Report "no problems so far, not
  yet cleared" and the expected clearance time. DeployAngel keeps verifying and
  alerts on failure.
- Exit 7: warnings at the initial check. Report them and review the findings.
  The release is NOT cleared.
- Exit 2 / inconclusive: the release is NOT verified. Do not claim success.
- Exit 1 / failed: read the findings and exceptions (`get_exception`),
  investigate the likely cause, and propose a fix. `next_steps` names the last
  cleared release a rollback would go to. Do not roll back or change
  production without explicit approval.
- Exit 3: still verifying; run the command again.
- Not cleared yet: call `get_exercise_plan` (or `{command} plan`).
  If its status is "exercisable" or "waiting_for_activity", run
  `{command} exercise --url=<production URL>`: it sends the
  plan's read-only requests and records them on the release. Offer to
  exercise what it skips: routes that change data only with a test account
  or after asking. If the status is "warm_up", nothing run can clear it.
- Confidence `medium` clears a release just as `high` does (high also needs 80%
  coverage and a full baseline), so don't call a medium clearance weak.
  Warnings (`suspicious: true`) aren't failures: report them as "watching
  closely:" and the `watching_closely` items, never as "suspicious".
- After deploying a fix for a failed release, read `fix_check` in the verdict
  (`--format=json` with the CLI): `fixed` means every earlier problem is
  healthy again. Otherwise report what's still happening or not confirmed yet.
- At the start of a session, before changing code: call `list_deployments`
  and `list_late_regressions` (or run `{command} status`). If a recent release
  failed, isn't cleared yet, or has a late regression, mention it before other
  work.
{AGENTS_END}
"""


# After `deployangel setup`, the MCP server and CLI here use its saved token.
AGENTS_SAVED_TOKEN_HELP = """Here, the MCP server and CLI use the token `deployangel setup` saved in .deployangel/.
Teammates redeem their own setup code, or set DEPLOYANGEL_API_TOKEN to a
"CLI & coding agents" token from the app's Settings, for example in an .envrc
with direnv. These files hold no token and are meant to be committed.
Codex reads .codex/config.toml only in projects you've marked as trusted."""
AGENTS_TOKEN_HELP = """The MCP server and CLI need DEPLOYANGEL_API_TOKEN, a "CLI & coding agents" token
from the app's Settings, in the environment your agent runs in (for example in
an .envrc with direnv), or a token saved here by `deployangel setup`.
Never put it in these files: they're meant to be committed.
Codex reads .codex/config.toml only in projects you've marked as trusted."""

# Added at the end of the Dockerfile's last stage: a value that changes with
# every commit invalidates the cache of every layer after it, so it goes
# after the dependency installs.
DOCKERFILE_LINES = """# The commit this image runs, for DeployAngel. Build with --build-arg GIT_SHA=$(git rev-parse HEAD).
ARG GIT_SHA
ENV DEPLOYANGEL_REVISION=$GIT_SHA
"""
DOCKER_BUILD_HELP = """Pass the commit when you build the image:

  docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .
  fly deploy --build-arg GIT_SHA=$(git rev-parse HEAD)
  GitHub Actions (docker/build-push-action):
    build-args: GIT_SHA=${{ github.sha }}

Kamal apps don't need this: DeployAngel reads Kamal's KAMAL_VERSION."""


class UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Reports bad options as usage errors (exit 5), never by exiting itself."""

    def error(self, message):
        raise UsageError(message)


class CLI:
    def __init__(self, argv, env: Optional[Mapping[str, str]] = None, stdin=None, stdout=None, stderr=None,
                 client=None, sleeper: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
                 git_head=None, root: Optional[str] = None, requester=None):
        self.argv = list(argv)
        self.env = os.environ if env is None else env
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self._client = client
        self.sleeper = sleeper
        self.clock = clock
        # None looks the commit up with git; False means there is none.
        self._git_head = git_head
        self.root = root or os.getcwd()
        self.requester = requester

    def run(self) -> int:
        command = self.argv.pop(0) if self.argv else None
        commands = {"release": self.release, "verify": self.verify, "status": lambda: self.verify(status=True),
                    "plan": self.plan, "exercise": self.exercise, "exception": self.exception, "check": self.check, "install": self.install,
                    "doctor": self.doctor, "setup": self.setup, "token": self.token, "mcp": self.mcp}
        try:
            if command in commands:
                return commands[command]()
            if command in ("version", "--version", "-v"):
                self._print(VERSION)
                return 0
            if command in (None, "help", "--help", "-h"):
                self._print(HELP)
                return 0
            return self._usage_error(f"unknown command: {command}")
        except UsageError as error:
            return self._usage_error(str(error))
        except Unauthorized as error:
            self._error(str(error))
            return USAGE_ERROR
        except NotFound as error:
            self._error(str(error))
            return NOT_FOUND
        except ApiError as error:
            self._error(str(error))
            return USAGE_ERROR

    def release(self) -> int:
        options = self._parse([("--commit",), ("--version",), ("--kind",), ("--provider",), ("--url", "source_url")])
        # Explicit options win, then the CI system, then git.
        detected = ci.detect(self.env)
        if detected:
            options["commit"] = options["commit"] or detected.commit
            options["version"] = options["version"] or detected.version
            options["provider"] = options["provider"] or detected.provider
            options["source_url"] = options["source_url"] or detected.source_url
        options["commit"] = options["commit"] or self.git_head()
        if not options["commit"] and not options["version"]:
            return self._usage_error("release needs --commit or --version (no git repository or CI commit found)")
        deployment = self.client().register_deployment(**options)
        self._print(f"Registered deployment {deployment.get('id')} "
                    f"({deployment.get('version') or deployment.get('commit')}), verification {deployment.get('state')}")
        return 0

    def verify(self, status: bool = False) -> int:
        parser = self._parser([("--commit",), ("--version",), ("--deployment", "deployment_id")])
        parser.add_argument("--wait", action="store_true")
        parser.add_argument("--until", dest="until_mode", choices=UNTIL_MODES, default="verdict")
        parser.add_argument("--timeout", default="1800")
        parser.add_argument("--format", choices=("text", "json"))
        parser.add_argument("--all-findings", action="store_true")
        options = vars(parser.parse_args(self.argv))
        timeout = _duration(options["timeout"])
        target = {"latest": True} if status else self._target(options)
        if not target:
            return self._usage_error("no target: pass --commit, --version, or --deployment, or run inside a git repository")

        progress = (lambda message: self.stderr.write(message + "\n")) if options["wait"] else None
        waiter = Waiter(self.client(), sleeper=self.sleeper, clock=self.clock, on_progress=progress)
        outcome = waiter.wait(target, until_mode=options["until_mode"], timeout=timeout, wait=options["wait"])
        if outcome.not_found:
            described = next(iter(target.values()))
            self._error(f"no deployment found for {described}")
            self._step_summary(f"### DeployAngel: no deployment found for {described}\n")
            return outcome.exit_code

        document = outcome.document
        if options["all_findings"]:
            document = self.client().verification(document["deployment"]["id"], all_findings=True)
        self._output(document, options["format"], lambda: formatter.verification(document))
        self._step_summary(formatter.markdown(document))
        if outcome.timed_out:
            self._error("timed out; verification is still in progress")
        return outcome.exit_code

    def plan(self) -> int:
        """The release's exercise plan: what stands between it and clearance,
        and what to exercise against production so it clears sooner."""
        parser = self._parser([("--commit",), ("--version",), ("--deployment", "deployment_id")])
        parser.add_argument("--format", choices=("text", "json"))
        options = vars(parser.parse_args(self.argv))
        target = self._target(options)
        if not target:
            return self._usage_error("no target: pass --commit, --version, or --deployment, or run inside a git repository")

        outcome = Waiter(self.client(), sleeper=self.sleeper, clock=self.clock).wait(target, wait=False)
        if outcome.not_found:
            self._error(f"no deployment found for {next(iter(target.values()))}")
            return outcome.exit_code
        document = outcome.document
        data = {"deployment": document.get("deployment"), "exercise_plan": document.get("exercise_plan")}
        self._output(data, options["format"], lambda: formatter.exercise_plan(document))
        return 0

    def exercise(self) -> int:
        """Sends the plan's read-only requests from here, then records what it
        sent on the release (spec §16, Exercise records)."""
        from datetime import datetime, timezone

        from deployangel.cli.exerciser import MAX_REQUESTS, Exerciser

        parser = self._parser([("--url",), ("--commit",), ("--version",), ("--deployment", "deployment_id")])
        parser.add_argument("--max-requests", dest="max_requests", type=int, default=MAX_REQUESTS)
        parser.add_argument("--dry-run", dest="dry_run", action="store_true")
        options = vars(parser.parse_args(self.argv))
        if not re.match(r"\Ahttps?://[^/\s]+", options["url"] or ""):
            return self._usage_error("exercise needs --url, the app's production URL, like https://example.com")
        target = self._target(options)
        if not target:
            return self._usage_error("no target: pass --commit, --version, or --deployment, or run inside a git repository")

        outcome = Waiter(self.client(), sleeper=self.sleeper, clock=self.clock).wait(target, wait=False)
        if outcome.not_found:
            self._error(f"no deployment found for {next(iter(target.values()))}")
            return outcome.exit_code
        document = outcome.document
        plan = document.get("exercise_plan") or {}
        if plan.get("status") not in ("exercisable", "waiting_for_activity"):
            self._print(f"Nothing to exercise. {plan.get('summary') or ''}".rstrip())
            return 0

        exerciser = Exerciser(options["url"], max_requests=min(max(options["max_requests"], 1), MAX_REQUESTS),
                              sleeper=self.sleeper, requester=self.requester)
        if options["dry_run"]:
            targets, skipped = exerciser.plan(plan)
            lines = [f"Would send {sum(t.count for t in targets)} requests:"]
            lines += [f"  {exerciser.url_for(t.path)} ×{t.count}" for t in targets]
            if skipped:
                lines += ["Skipped:"] + [f"  {s['key']} ({s['reason']})" for s in skipped]
            self._print("\n".join(lines))
            return 0

        result = exerciser.run(plan)
        lines = [f"Sent {result.sent} requests to {options['url']}:"]
        # "200 ×4, 500 ×1", in the order they came.
        lines += [f"  {r['key']} ×{r['requests']}: " + ", ".join(f"{'no answer' if code is None else code} ×{n}"
                                                             for code, n in result.codes[r["key"]].items()) for r in result.routes]
        if result.skipped:
            lines += ["Skipped:"] + [f"  {s['key']} ({s['reason']})" for s in result.skipped]
        if result.unreachable:
            lines.append(f"Stopped early: {options['url']} isn't answering.")
        # Requests that reached no page aren't worth recording: the app likely
        # never saw them.
        if result.routes and result.nothing_served() and not result.unreachable:
            lines += ["", f"No page answered at {options['url']}: every route said it doesn't exist. Is that the app's own host, "
                          "and not a marketing site or a different domain? Pass the URL where the app's pages load."]
            if result.unsent:
                lines.append(f"{result.unsent} more requests weren't sent, since no route answered.")
            lines.append("Nothing was recorded on the release.")
            self._print("\n".join(lines))
            return USAGE_ERROR
        if result.routes:
            ran_at = datetime.fromtimestamp(self.clock(), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            self.client().record_exercise((document.get("deployment") or {}).get("id"), routes=result.routes,
                                          skipped=result.skipped, ran_at=ran_at)
            lines.append("Recorded on the release. Wait for the verdict with: deployangel verify --wait")
        self._print("\n".join(lines))
        return USAGE_ERROR if result.unreachable else 0

    def exception(self) -> int:
        if not self.argv or self.argv[0].startswith("-"):
            return self._usage_error("exception needs a FINGERPRINT")
        fingerprint = self.argv.pop(0)
        parser = self._parser([])
        parser.add_argument("--format", choices=("text", "json"))
        options = vars(parser.parse_args(self.argv))
        details = self.client().exception(fingerprint)
        self._output(details, options["format"], lambda: formatter.exception(details))
        return 0

    def check(self) -> int:
        parser = self._parser([("--name",), ("--details-url", "details_url"), ("--commit",), ("--deployment", "deployment_id")])
        parser.add_argument("--status", choices=("pass", "fail"))
        parser.add_argument("--covers", default="")
        options = vars(parser.parse_args(self.argv))
        if not options["name"] or not options["status"]:
            return self._usage_error("check needs --name and --status=pass|fail")

        commit = options["commit"]
        if not options["deployment_id"] and not commit:
            detected = ci.detect(self.env)
            commit = self.git_head() or (detected.commit if detected else None)
        reference = options["deployment_id"] or (f"commit:{commit}" if commit else None)
        if not reference:
            return self._usage_error("check needs --deployment or --commit, or a git repository")

        covers = [item.strip() for item in options["covers"].split(",") if item.strip()]
        result = self.client().report_check(reference, name=options["name"], status=options["status"],
                                            covers=covers, details_url=options["details_url"])
        self._print(f'Recorded {options["status"]} check "{options["name"]}" for deployment {result.get("deployment_id")}')
        return 0

    def install(self) -> int:
        target = self.argv.pop(0) if self.argv else None
        if target == "docker":
            return self._install_docker()
        if target == "agents":
            return self._install_agents()
        if target != "kamal":
            return self._usage_error("install needs a target: deployangel install kamal, docker, or agents")
        return self._install_kamal()

    def _install_agents(self) -> int:
        """Sets up Claude Code, Cursor, and Codex in this project: the MCP
        server in each one's project config, and instructions to wait for a
        verdict after deploying. Never overwrites what's there: an existing
        entry is kept, and a file it can't read is left for you to edit."""
        command = self._project_command() + ["mcp"]
        lines = [
            self._install_json_mcp(".mcp.json", command, "Claude Code"),
            self._install_json_mcp(os.path.join(".cursor", "mcp.json"), command, "Cursor"),
            self._install_codex_mcp(command),
            *self._install_agent_instructions(" ".join(command[:-1])),
        ]
        help_text = AGENTS_SAVED_TOKEN_HELP if credentials.find(self.root) else AGENTS_TOKEN_HELP
        self._print("\n".join(lines) + "\n\n" + help_text)
        return 0

    def _project_command(self) -> list:
        """How an agent runs this project's deployangel: through uv or Poetry
        when the project uses one, since it's installed in their environment."""
        if os.path.exists(os.path.join(self.root, "uv.lock")):
            return ["uv", "run", "deployangel"]
        if os.path.exists(os.path.join(self.root, "poetry.lock")):
            return ["poetry", "run", "deployangel"]
        return ["deployangel"]

    def _install_json_mcp(self, relative: str, command: list, agent: str) -> str:
        path = os.path.join(self.root, relative)
        exists = os.path.exists(path)
        try:
            config = {}
            if exists:
                with open(path) as file:
                    config = json.load(file)
            if not isinstance(config, dict):
                raise ValueError("not a JSON object")
        except ValueError:
            return (f"Couldn't read {relative}, so it's unchanged. Add a \"deployangel\" server to its mcpServers: "
                    f"command \"{command[0]}\", args {json.dumps(command[1:], separators=(',', ':'))}.")
        servers = config.setdefault("mcpServers", {})
        if "deployangel" in servers:
            return f"{relative} already has a deployangel MCP server ({agent})."
        servers["deployangel"] = {"command": command[0], "args": command[1:]}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as file:
            file.write(json.dumps(config, indent=2) + "\n")
        return f"{'Updated' if exists else 'Created'} {relative}: the DeployAngel MCP server for {agent}."

    def _install_codex_mcp(self, command: list) -> str:
        relative = os.path.join(".codex", "config.toml")
        path = os.path.join(self.root, relative)
        existing = None
        if os.path.exists(path):
            with open(path) as file:
                existing = file.read()
        if existing is not None and "[mcp_servers.deployangel]" in existing:
            return f"{relative} already has a deployangel MCP server (Codex)."
        table = ("[mcp_servers.deployangel]\n"
                 f"command = {json.dumps(command[0])}\n"
                 f"args = {json.dumps(command[1:])}\n"
                 'env_vars = ["DEPLOYANGEL_API_TOKEN", "DEPLOYANGEL_URL"]\n')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as file:
            file.write(existing.rstrip("\n") + "\n\n" + table if existing is not None else table)
        return f"{'Updated' if existing is not None else 'Created'} {relative}: the DeployAngel MCP server for Codex."

    def _install_agent_instructions(self, command: str) -> list:
        """AGENTS.md serves Codex and Cursor. Claude Code reads CLAUDE.md, so it
        gets the block too, unless it already imports AGENTS.md; a project
        without one gets a CLAUDE.md that does."""
        block = agent_instructions(command)
        lines = [self._upsert_instructions("AGENTS.md", block)]
        claude = os.path.join(self.root, "CLAUDE.md")
        if not os.path.exists(claude):
            with open(claude, "w") as file:
                file.write("@AGENTS.md\n")
            lines.append("Created CLAUDE.md, which imports AGENTS.md for Claude Code.")
        else:
            with open(claude) as file:
                imports = re.search(r"^@AGENTS\.md\s*$", file.read(), re.MULTILINE)
            if not imports:
                lines.append(self._upsert_instructions("CLAUDE.md", block))
        return lines

    def _upsert_instructions(self, relative: str, block: str) -> str:
        path = os.path.join(self.root, relative)
        if not os.path.exists(path):
            with open(path, "w") as file:
                file.write(block)
            return f"Created {relative} with instructions to wait for DeployAngel's verdict after deploying."
        with open(path) as file:
            content = file.read()
        pattern = re.compile(re.escape(AGENTS_START) + r".*?" + re.escape(AGENTS_END) + r"\n?", re.DOTALL)
        if pattern.search(content):
            updated = pattern.sub(lambda _: block, content, count=1)
            if updated == content:
                return f"{relative} already has DeployAngel's instructions."
            with open(path, "w") as file:
                file.write(updated)
            return f"Updated DeployAngel's instructions in {relative}."
        with open(path, "w") as file:
            file.write(content.rstrip("\n") + "\n\n" + block)
        return f"Added instructions to wait for DeployAngel's verdict after deploying to {relative}."

    def _install_kamal(self) -> int:
        """Writes .kamal/hooks/post-deploy, or says what to add to a hook that
        already exists, rather than overwriting it."""
        relative = os.path.join(".kamal", "hooks", "post-deploy")
        path = os.path.join(self.root, relative)
        if os.path.exists(path):
            with open(path) as hook:
                if "deployangel release" in hook.read():
                    self._print(f"{relative} already registers deploys with DeployAngel.")
                else:
                    self._print(f"{relative} already exists. Add this line to it:\n\n  {KAMAL_HOOK_LINE}")
            return 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as hook:
            hook.write(KAMAL_HOOK)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        self._print(f"Created {relative}. Each `kamal deploy` now registers its release with DeployAngel.\n"
                    'Set DEPLOYANGEL_API_TOKEN (a "CI deploys" token) wherever you run kamal deploy.')
        return 0

    def _install_docker(self) -> int:
        """Bakes the commit into the image as DEPLOYANGEL_REVISION, from a build
        argument. It never edits CI workflows; it says what to pass instead."""
        path = os.path.join(self.root, "Dockerfile")
        try:
            with open(path) as file:
                dockerfile = file.read()
        except FileNotFoundError:
            self._error(f"no Dockerfile in {self.root}; run this where your Dockerfile is")
            return USAGE_ERROR
        if "DEPLOYANGEL_REVISION" in dockerfile:
            self._print("Dockerfile already sets DEPLOYANGEL_REVISION.")
            return 0
        with open(path, "w") as file:
            file.write(_add_revision(dockerfile))
        self._print("Added DEPLOYANGEL_REVISION to the Dockerfile's last stage. " + DOCKER_BUILD_HELP)
        return 0

    def doctor(self) -> int:
        """What this checkout will report and what DeployAngel has seen of the
        app. Warnings are advice, so only errors (a rejected token, an
        unreachable server, no agent in the dependencies) fail it."""
        from deployangel.cli.doctor import Doctor

        parser = self._parser([])
        parser.add_argument("--format", choices=("text", "json"))
        options = vars(parser.parse_args(self.argv))
        doctor = Doctor(self.root, self._doctor_client(), self.env)
        self._output(doctor.to_dict(), options["format"], lambda: _doctor_text(doctor.checks))
        return USAGE_ERROR if doctor.failed else 0

    def _doctor_client(self):
        """Either token works: the setup endpoint answers any of the app's. The
        agent's own token is for running it where the app runs."""
        if self._client is not None:
            return self._client
        candidates = (self.env.get("DEPLOYANGEL_API_TOKEN"), self.env.get("DEPLOYANGEL_TOKEN"), self._saved().get("api_token"))
        token = next((value for value in candidates if value), None)
        return Client(token, self._endpoint()) if token else None

    def mcp(self) -> int:
        from deployangel.cli.mcp import Server

        # Without a token the server still starts, and each tool says how to
        # set one: many agents only report a server that won't start as
        # "failed to start", with no reason.
        token_missing = self._client is None and not (self.env.get("DEPLOYANGEL_API_TOKEN") or self._saved().get("api_token"))
        if token_missing:
            self.stderr.write("deployangel mcp: no API token yet; each tool will say how to set one.\n")
            self._client = Client(None, self._endpoint(), require_token=False)
        Server(self.client(), input=self.stdin, output=self.stdout, error_output=self.stderr,
               sleeper=self.sleeper, clock=self.clock, git_head=self.git_head, token_missing=token_missing).run()
        return 0

    def client(self):
        """DEPLOYANGEL_API_TOKEN, else the token `deployangel setup` saved here."""
        if self._client is None:
            token = self.env.get("DEPLOYANGEL_API_TOKEN") or self._saved().get("api_token")
            self._client = Client(token, self._endpoint())
        return self._client

    def _endpoint(self) -> str:
        return self.env.get("DEPLOYANGEL_URL") or self._saved().get("endpoint") or DEFAULT_ENDPOINT

    def _saved(self) -> dict:
        if getattr(self, "_saved_credentials", None) is None:
            self._saved_credentials = credentials.read(self.root)
        return self._saved_credentials

    def setup(self) -> int:
        """Redeems a one-time code from "Set up with your coding agent" for the
        app's two tokens, and saves them without printing either, so they stay
        out of a coding agent's conversation."""
        parser = self._parser([("--code",)])
        options = vars(parser.parse_args(self.argv))
        if not options["code"]:
            return self._usage_error('setup needs --code (from "Set up with your coding agent" on the app\'s page)')
        redeemer = self._client or Client(None, self._endpoint(), require_token=False)
        result = redeemer.redeem_setup_code(options["code"].strip())
        app = result.get("app") or {}
        file = credentials.write(self.root, {"app": app, "app_token": result.get("app_token"),
                                             "api_token": result.get("api_token"), "endpoint": self._endpoint()})
        self._saved_credentials = None
        relative = os.path.relpath(file, self.root)
        command = self._command_name()
        self._print(f"""Set up DeployAngel for {app.get('name')} ({app.get('environment')}). Saved its app token and a "CI deploys" token to
{relative}, kept out of git and not shown here. The deployangel command and MCP server here use them.

The running app reads its token only from DEPLOYANGEL_TOKEN in its environment, so give it the app
token without printing it: $({command} token app) in the command that starts it or sets your
host's secrets, or {command} token app | <a command that reads a secret>. The same goes for CI and
the "CI deploys" token ({command} token api). Steps for each host:
https://www.deployangel.com/setup-for-agents.md""")
        return 0

    def token(self) -> int:
        """Prints a saved token, for piping into a host's or CI's secrets.
        Never run it bare where its output is shown, such as an agent's chat."""
        kind = self.argv.pop(0) if self.argv else None
        if kind not in ("app", "api"):
            return self._usage_error("token needs app or api: deployangel token app")
        value = self._saved().get(f"{kind}_token")
        if not value:
            return self._usage_error("no token saved here; run deployangel setup --code=<code> first")
        self.stdout.write(value + ("\n" if _isatty(self.stdout) else ""))
        self.stdout.flush()
        return 0

    def _command_name(self) -> str:
        if os.path.exists(os.path.join(self.root, "uv.lock")):
            return "uv run deployangel"
        if os.path.exists(os.path.join(self.root, "poetry.lock")):
            return "poetry run deployangel"
        return "deployangel"

    def git_head(self) -> Optional[str]:
        if self._git_head:
            return self._git_head
        if self._git_head is False:
            return None
        try:
            result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.root, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None

    def _parser(self, string_options) -> _Parser:
        parser = _Parser(prog="deployangel", add_help=False, allow_abbrev=False)
        for option in string_options:
            flag = option[0]
            dest = option[1] if len(option) > 1 else flag.lstrip("-").replace("-", "_")
            parser.add_argument(flag, dest=dest)
        return parser

    def _parse(self, string_options) -> dict:
        return vars(self._parser(string_options).parse_args(self.argv))

    def _target(self, options: dict) -> Optional[dict]:
        if options.get("deployment_id"):
            return {"deployment_id": options["deployment_id"]}
        if options.get("version"):
            return {"version": options["version"]}
        detected = ci.detect(self.env)
        commit = options.get("commit") or self.git_head() or (detected.commit if detected else None)
        return {"commit": commit.lower()} if commit else None

    def _output(self, data, format: Optional[str], text: Callable[[], str]) -> None:
        if format is None:
            format = "text" if _isatty(self.stdout) else "json"
        self._print(json.dumps(data, indent=2, ensure_ascii=False) if format == "json" else text())

    def _step_summary(self, markdown: str) -> None:
        """In GitHub Actions, the verdict also goes on the run's summary page."""
        path = str(self.env.get("GITHUB_STEP_SUMMARY") or "")
        if not path:
            return
        try:
            with open(path, "a") as summary:
                summary.write(markdown if markdown.endswith("\n") else markdown + "\n")
        except OSError as error:
            self._error(f"couldn't write the job summary ({error.strerror or error})")

    def _print(self, text: str) -> None:
        self.stdout.write(text + "\n")
        self.stdout.flush()

    def _error(self, message: str) -> None:
        self.stderr.write(f"deployangel: {message}\n")

    def _usage_error(self, message: str) -> int:
        self._error(message)
        self.stderr.write("Run `deployangel help` for usage.\n")
        return USAGE_ERROR


def _duration(value: str) -> int:
    match = re.fullmatch(r"(\d+)(s|m|h)?", str(value))
    if not match:
        raise UsageError(f"invalid argument: --timeout={value}")
    return int(match.group(1)) * {None: 1, "s": 1, "m": 60, "h": 3600}[match.group(2)]


def _add_revision(dockerfile: str) -> str:
    """Inserts DOCKERFILE_LINES in the last stage, just before the CMD and
    ENTRYPOINT instructions that end it, or at the end of the file."""
    lines = dockerfile.splitlines(keepends=True)
    # (first line, instruction) for each instruction, joining continued lines.
    instructions = []
    continued = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if continued:
            continued = stripped.endswith("\\")
            continue
        if not stripped or stripped.startswith("#"):
            continue
        instructions.append((index, stripped.split(None, 1)[0].upper()))
        continued = stripped.endswith("\\")
    insert_at = None
    for index, keyword in reversed(instructions):
        if keyword not in ("CMD", "ENTRYPOINT"):
            break
        insert_at = index
    # A comment just above CMD describes it, so it stays with it.
    while insert_at is not None and insert_at > 0 and lines[insert_at - 1].strip().startswith("#"):
        insert_at -= 1
    if insert_at is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        return "".join(lines) + ("\n" if lines and lines[-1].strip() else "") + DOCKERFILE_LINES
    return "".join(lines[:insert_at]) + DOCKERFILE_LINES + "\n" + "".join(lines[insert_at:])


DOCTOR_MARKS = {"ok": "✓", "warn": "!", "error": "✗", "info": "·"}


def _doctor_text(checks: list) -> str:
    lines = ["DeployAngel doctor"]
    for check in checks:
        lines.append(f"  {DOCTOR_MARKS[check.status]} {check.message}")
        if check.fix:
            lines.append(f"      {check.fix}")
    return "\n".join(lines)


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv=None) -> None:
    sys.exit(CLI(sys.argv[1:] if argv is None else argv).run())
