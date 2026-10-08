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

from deployangel.cli import ci, formatter
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
  exception  Details for a fingerprint      deployangel exception FINGERPRINT
  check      Report a smoke test result     --name=NAME --status=pass|fail [--covers=a,b]
                                            [--commit=SHA | --deployment=ID] [--details-url=URL]
  install    Add a Kamal deploy hook, or    deployangel install kamal
             bake the commit into an image  deployangel install docker
  mcp        Run the MCP server over stdio (for coding agents)

With no target, verify, plan, and check use the current git HEAD commit.
In GitHub Actions, GitLab CI, CircleCI, and Buildkite, release fills in the
commit, a build label (e.g. run-123), and a link to the run automatically.
In a Kamal hook, it registers Kamal's release (KAMAL_VERSION).
Environment: DEPLOYANGEL_API_TOKEN (required), DEPLOYANGEL_URL (default {DEFAULT_ENDPOINT})

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
                 git_head=None, root: Optional[str] = None):
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

    def run(self) -> int:
        command = self.argv.pop(0) if self.argv else None
        commands = {"release": self.release, "verify": self.verify, "status": lambda: self.verify(status=True),
                    "plan": self.plan, "exception": self.exception, "check": self.check, "install": self.install,
                    "mcp": self.mcp}
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
        if target != "kamal":
            return self._usage_error("install needs a target: deployangel install kamal, or deployangel install docker")
        return self._install_kamal()

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

    def mcp(self) -> int:
        from deployangel.cli.mcp import Server

        Server(self.client(), input=self.stdin, output=self.stdout, error_output=self.stderr,
               sleeper=self.sleeper, clock=self.clock, git_head=self.git_head).run()
        return 0

    def client(self):
        if self._client is None:
            self._client = Client(self.env.get("DEPLOYANGEL_API_TOKEN"), self.env.get("DEPLOYANGEL_URL") or DEFAULT_ENDPOINT)
        return self._client

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


def _isatty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv=None) -> None:
    sys.exit(CLI(sys.argv[1:] if argv is None else argv).run())
