"""The deployangel command and MCP server, ported case for case from the Ruby
gem's specs so both commands behave the same: options, output, exit codes."""

import io
import json
import os

import pytest

from deployangel.cli import CLI, ci
from deployangel.cli.client import NotFound
from deployangel.cli.mcp import Server

START = 1790776800.0  # 2026-09-30 14:00 UTC


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeClient:
    """Each verification call returns the next document, then keeps returning the last one."""

    def __init__(self, documents=(), deployments_list=None, scopes=("verifications:read",)):
        self.documents = list(documents)
        self.deployments_list = [{"id": 42}] if deployments_list is None else deployments_list
        self.scopes = list(scopes)
        self.calls = []

    def token_info(self):
        self.calls.append(("token_info",))
        return {"scopes": self.scopes}

    def latest_deployment(self):
        self.calls.append(("latest",))
        if not self.deployments_list:
            raise NotFound("not found")
        return self.deployments_list[0]

    def deployments(self, **filters):
        self.calls.append(("deployments", filters))
        return self.deployments_list

    def verification(self, deployment_id, all_findings=False):
        self.calls.append(("verification", deployment_id))
        return self.documents.pop(0) if len(self.documents) > 1 else self.documents[0]

    def register_deployment(self, **attributes):
        self.calls.append(("register", attributes))
        return {"id": 43, "version": attributes.get("version"), "commit": attributes.get("commit"), "state": "pending"}

    def report_check(self, reference, **attributes):
        self.calls.append(("check", reference, attributes))
        return {"id": 1, "status": attributes["status"], "deployment_id": 42}

    def exception(self, fingerprint):
        return {"fingerprint": fingerprint, "exception_class": "NoMethodError", "backtrace": []}

    def late_regressions(self, **filters):
        return []


def verdict_document(state, verdict=None, initial_check=None, poll=60):
    return {"schema_version": 1, "deployment": {"id": 42, "version": "v184"},
            "verification": {"state": state, "verdict": verdict, "initial_check": initial_check, "confidence": "high", "coverage": 1.0},
            "clearance": {"statement": f"v184 {verdict or 'not cleared yet'}", "missing_evidence": [], "not_observable": [], "still_watching": []},
            "findings": [], "poll_after_seconds": poll, "dashboard_url": "https://app.deployangel.com/apps/1/deployments/42"}


def exercise_plan(status="exercisable"):
    return {"status": status, "summary": "Not cleared yet. Exercising these items against production would let it clear sooner.",
            "shortfall": {"rule": "low_volume", "requests": {"have": 12, "need": 30},
                          "routes_run_3_times": {"have": 1, "need": 3, "of": 3}},
            "items": [
                {"kind": "route", "key": "GET /orders/<int:pk>/", "reason": "normally_active", "runs": 1, "runs_needed": 3, "mutating": False,
                 "needed": True},
                {"kind": "route", "key": "POST /password_resets/", "reason": "changed_in_release", "runs": 0, "mutating": True, "needed": False},
                {"kind": "job_class", "key": "send_invoice", "reason": "normally_active", "runs": 0, "triggered_by": "app_behavior",
                 "needed": True},
            ],
            "report_with": 'deployangel check --name="exercise plan" --status=pass --covers="GET /orders/<int:pk>/,POST /password_resets/,send_invoice"'}


@pytest.fixture
def io_streams():
    return io.StringIO(), io.StringIO()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def run(io_streams, clock):
    stdout, stderr = io_streams

    def run(*argv, client, git_head="81ac27d0000", env=None):
        return CLI(list(argv), env=env or {}, stdout=stdout, stderr=stderr, client=client, sleeper=clock.advance,
                   clock=clock, git_head=git_head).run()

    run.stdout, run.stderr = stdout, stderr
    return run


def test_maps_verdicts_to_exit_codes(run):
    for verdict, code in {"verified": 0, "failed": 1, "inconclusive": 2}.items():
        assert run("verify", "--format=json", client=FakeClient([verdict_document("closed", verdict)])) == code


def test_defaults_to_the_git_head_commit(run):
    client = FakeClient([verdict_document("closed", "verified")])
    run("verify", client=client)
    assert client.calls[0] == ("deployments", {"commit": "81ac27d0000", "version": None, "limit": 1})


def test_exits_3_without_waiting_while_in_progress(run):
    assert run("verify", client=FakeClient([verdict_document("observing")])) == 3


def test_waits_for_a_verdict_printing_progress_to_stderr(run):
    client = FakeClient([verdict_document("pending"), verdict_document("observing"), verdict_document("closed", "verified")])
    assert run("verify", "--wait", "--format=json", client=client) == 0
    assert all(text in run.stderr.getvalue() for text in ("v184: pending", "v184: observing", "v184: closed"))
    assert json.loads(run.stdout.getvalue())["verification"]["verdict"] == "verified"


def test_returns_at_the_initial_check_never_as_success(run):
    ok = FakeClient([verdict_document("observing"), verdict_document("observing", initial_check={"result": "no_problems_so_far"})])
    warn = FakeClient([verdict_document("observing", initial_check={"result": "warnings"})])
    assert run("verify", "--wait", "--until=initial", client=ok) == 6
    assert run("verify", "--wait", "--until=initial", client=warn) == 7


def test_failed_returns_immediately_even_when_waiting_through_watching(run):
    assert run("verify", "--wait", "--until=closed", client=FakeClient([verdict_document("closed", "failed")])) == 1


def test_keeps_waiting_through_watching_with_until_closed(run):
    client = FakeClient([verdict_document("watching", "verified"), verdict_document("closed", "verified")])
    assert run("verify", "--wait", "--until=closed", client=client) == 0
    assert sum(1 for call in client.calls if call[0] == "verification") == 2


def test_times_out_with_exit_3_and_the_current_document(run, clock):
    client = FakeClient([verdict_document("observing")])
    assert run("verify", "--wait", "--timeout=5m", "--format=json", client=client) == 3
    assert "timed out" in run.stderr.getvalue()
    assert clock.now == START + 300


def test_waits_for_the_deployment_to_be_registered_or_exits_4(run, io_streams, clock):
    assert run("verify", client=FakeClient(deployments_list=[])) == 4

    appears = FakeClient([verdict_document("closed", "verified")], deployments_list=[])

    def sleeper(seconds):
        clock.advance(seconds)
        appears.deployments_list = [{"id": 42}]

    stdout, stderr = io_streams
    code = CLI(["verify", "--wait"], env={}, stdout=stdout, stderr=stderr, client=appears, sleeper=sleeper, clock=clock,
               git_head="81ac27d").run()
    assert code == 0
    assert "to be registered" in stderr.getvalue()


def test_renders_a_readable_summary(run):
    document = verdict_document("closed", "failed")
    document["findings"] = [
        {"signal": "http_5xx_rate", "scope": "application", "status": "failing", "baseline_value": 0.002,
         "observed_value": 0.068, "observed_n": 2140},
        {"signal": "missing_recurring_job", "scope": "recurring_job:prune_history", "status": "failing",
         "baseline_value": 86400.0, "observed_value": None, "threshold": "expected by 08:15 UTC (declared schedule)"},
    ]
    document["exceptions"] = [{"exception_class": "KeyError", "top_frame": "app/orders.py#create", "count": 8,
                               "sources": {"route:POST /orders/": 8}}]
    document["deployment"]["promoted_from"] = {"environment": "staging", "version": "v57", "verdict": "verified"}
    run("verify", "--format=text", client=FakeClient([document]))

    out = run.stdout.getvalue()
    for text in ("HTTP 5xx rate on application: 0.2% -> 6.8% (2140 samples)",
                 "KeyError in app/orders.py#create (8x) route:POST /orders/", "Verdict: failed",
                 "Promoted from staging v57 (cleared)",
                 "Recurring job on recurring_job:prune_history: didn't run, expected by 08:15 UTC (declared schedule)"):
        assert text in out
    assert "8640000" not in out


def test_adds_the_verdict_to_the_github_actions_job_summary(run, tmp_path):
    document = verdict_document("closed", "failed")
    document["findings"] = [
        {"signal": "p95_latency", "scope": "route:GET /a|b", "status": "failing", "baseline_value": 120.0,
         "observed_value": 480.0, "observed_n": 900},
        {"signal": "job_failure_rate", "scope": "application", "status": "pass"},
    ]
    document["exceptions"] = [{"exception_class": "KeyError", "top_frame": "app/jobs.py#sync", "count": 3}]
    path = tmp_path / "summary.md"
    code = run("verify", "--format=json", client=FakeClient([document]), env={"GITHUB_STEP_SUMMARY": str(path)})

    assert code == 1
    summary = path.read_text()
    for text in ("### DeployAngel: v184 failed", "| failing | p95 latency on route:GET /a\\|b: 120 ms -> 480 ms (900 samples) |",
                 "**New exceptions**", "- KeyError in app/jobs.py#sync (3x)",
                 "[Open in DeployAngel](https://app.deployangel.com/apps/1/deployments/42)"):
        assert text in summary
    assert "Job failure rate" not in summary
    assert json.loads(run.stdout.getvalue())["verification"]["verdict"] == "failed"


def test_says_in_the_job_summary_when_an_initial_check_isnt_a_clearance(run, tmp_path):
    path = tmp_path / "summary.md"
    ok = FakeClient([verdict_document("observing", initial_check={"result": "no_problems_so_far"})])
    run("verify", "--until=initial", client=ok, env={"GITHUB_STEP_SUMMARY": str(path)})
    run("verify", client=FakeClient(deployments_list=[]), env={"GITHUB_STEP_SUMMARY": str(path)})

    summary = path.read_text()
    for text in ("### DeployAngel: v184 has no problems so far, not cleared yet",
                 "Initial check: no problems so far. Not cleared yet.", "### DeployAngel: no deployment found for 81ac27d0000"):
        assert text in summary


def test_still_exits_with_the_verdict_when_the_job_summary_cant_be_written(run):
    code = run("verify", "--format=json", client=FakeClient([verdict_document("closed", "verified")]),
               env={"GITHUB_STEP_SUMMARY": "/nonexistent/summary.md"})
    assert code == 0
    assert "couldn't write the job summary" in run.stderr.getvalue()


def test_registers_deployments_and_reports_checks_against_the_current_commit(run):
    client = FakeClient()
    assert run("release", "--version=v185", client=client) == 0
    assert run("check", "--name=smoke", "--status=pass", "--covers=password_reset", client=client) == 0
    assert ("register", {"commit": "81ac27d0000", "version": "v185", "kind": None, "provider": None, "source_url": None}) in client.calls
    assert ("check", "commit:81ac27d0000", {"name": "smoke", "status": "pass", "covers": ["password_reset"], "details_url": None}) in client.calls


def test_fills_in_commit_label_provider_and_link_inside_github_actions(io_streams):
    stdout, stderr = io_streams
    client = FakeClient()
    env = {"DEPLOYANGEL_API_TOKEN": "t", "GITHUB_ACTIONS": "true", "GITHUB_SHA": "abc1234def5678", "GITHUB_RUN_NUMBER": "12",
           "GITHUB_RUN_ID": "99", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "acme/shop"}
    assert CLI(["release"], env=env, stdout=stdout, stderr=stderr, client=client, git_head=False).run() == 0
    assert client.calls[-1] == ("register", {"commit": "abc1234def5678", "version": "run-12", "kind": None,
                                             "provider": "github_actions", "source_url": "https://github.com/acme/shop/actions/runs/99"})

    CLI(["release", "--version=2026.09.30", "--provider=manual"], env=env, stdout=stdout, stderr=stderr, client=client, git_head=False).run()
    assert client.calls[-1][1]["version"] == "2026.09.30"
    assert client.calls[-1][1]["provider"] == "manual"
    assert client.calls[-1][1]["commit"] == "abc1234def5678"


def test_registers_kamals_release_from_a_post_deploy_hook(io_streams):
    stdout, stderr = io_streams
    client = FakeClient()
    env = {"DEPLOYANGEL_API_TOKEN": "t", "KAMAL_VERSION": "abc1234def5678", "KAMAL_COMMAND": "deploy"}
    CLI(["release"], env=env, stdout=stdout, stderr=stderr, client=client, git_head=False).run()
    assert client.calls[-1] == ("register", {"commit": "abc1234def5678", "version": None, "kind": None, "provider": "kamal", "source_url": None})


def test_install_kamal_writes_an_executable_hook(io_streams, tmp_path):
    stdout, stderr = io_streams
    assert CLI(["install", "kamal"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    hook = tmp_path / ".kamal" / "hooks" / "post-deploy"
    assert "deployangel release || true" in hook.read_text()
    assert os.access(hook, os.X_OK)
    assert "Created .kamal/hooks/post-deploy" in stdout.getvalue() and "DEPLOYANGEL_API_TOKEN" in stdout.getvalue()


def test_install_kamal_leaves_an_existing_hook_alone(io_streams, tmp_path):
    stdout, stderr = io_streams
    hook = tmp_path / ".kamal" / "hooks" / "post-deploy"
    hook.parent.mkdir(parents=True)
    hook.write_text("#!/bin/sh\necho deployed\n")
    assert CLI(["install", "kamal"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    assert hook.read_text() == "#!/bin/sh\necho deployed\n"
    assert "already exists. Add this line to it:" in stdout.getvalue()


REVISION_LINES = ("# The commit this image runs, for DeployAngel. Build with --build-arg GIT_SHA=$(git rev-parse HEAD).\n"
                  "ARG GIT_SHA\nENV DEPLOYANGEL_REVISION=$GIT_SHA\n")


def test_install_docker_adds_the_revision_before_the_last_stages_cmd(io_streams, tmp_path):
    stdout, stderr = io_streams
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('FROM python:3.13 AS build\nRUN pip install -r requirements.txt\nCMD ["build"]\n\n'
                          'FROM python:3.13-slim\nCOPY --from=build /app /app\nEXPOSE 8000\n\n'
                          '# Start the server\nENTRYPOINT ["tini", "--"]\nCMD ["gunicorn", \\\n  "app.wsgi"]\n')
    assert CLI(["install", "docker"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    assert dockerfile.read_text() == (
        'FROM python:3.13 AS build\nRUN pip install -r requirements.txt\nCMD ["build"]\n\n'
        'FROM python:3.13-slim\nCOPY --from=build /app /app\nEXPOSE 8000\n\n'
        + REVISION_LINES + '\n# Start the server\nENTRYPOINT ["tini", "--"]\nCMD ["gunicorn", \\\n  "app.wsgi"]\n')
    out = stdout.getvalue()
    for text in ("docker build --build-arg GIT_SHA=$(git rev-parse HEAD) .", "fly deploy --build-arg GIT_SHA=$(git rev-parse HEAD)",
                 "build-args: GIT_SHA=${{ github.sha }}", "Kamal apps don't need this"):
        assert text in out


def test_install_docker_appends_when_the_last_stage_has_no_cmd(io_streams, tmp_path):
    stdout, stderr = io_streams
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('FROM python:3.13\nCMD ["early"]\nFROM python:3.13-slim\nRUN pip install gunicorn')
    assert CLI(["install", "docker"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    assert dockerfile.read_text() == 'FROM python:3.13\nCMD ["early"]\nFROM python:3.13-slim\nRUN pip install gunicorn\n\n' + REVISION_LINES


def test_install_docker_is_idempotent(io_streams, tmp_path):
    stdout, stderr = io_streams
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text('FROM python:3.13\nCMD ["gunicorn"]\n')
    assert CLI(["install", "docker"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    installed = dockerfile.read_text()
    assert CLI(["install", "docker"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 0
    assert dockerfile.read_text() == installed and installed.count("DEPLOYANGEL_REVISION") == 1
    assert "Dockerfile already sets DEPLOYANGEL_REVISION." in stdout.getvalue()


def test_install_docker_needs_a_dockerfile(io_streams, tmp_path):
    stdout, stderr = io_streams
    assert CLI(["install", "docker"], stdout=stdout, stderr=stderr, root=str(tmp_path)).run() == 5
    assert f"deployangel: no Dockerfile in {tmp_path}" in stderr.getvalue()
    assert not (tmp_path / "Dockerfile").exists()


def test_install_needs_a_known_target(io_streams):
    stdout, stderr = io_streams
    assert CLI(["install", "heroku"], stdout=stdout, stderr=stderr).run() == 5
    assert "deployangel install kamal, docker, or agents" in stderr.getvalue()


def _install_agents(io_streams, root):
    stdout, stderr = io_streams
    assert CLI(["install", "agents"], stdout=stdout, stderr=stderr, root=str(root)).run() == 0
    return stdout.getvalue()


def test_install_agents_sets_up_claude_code_cursor_and_codex_in_a_bare_project(io_streams, tmp_path):
    from deployangel.cli import agent_instructions

    out = _install_agents(io_streams, tmp_path)
    server = {"command": "deployangel", "args": ["mcp"]}
    assert json.loads((tmp_path / ".mcp.json").read_text()) == {"mcpServers": {"deployangel": server}}
    assert json.loads((tmp_path / ".cursor" / "mcp.json").read_text()) == {"mcpServers": {"deployangel": server}}
    assert (tmp_path / ".codex" / "config.toml").read_text() == (
        '[mcp_servers.deployangel]\ncommand = "deployangel"\nargs = ["mcp"]\n'
        'env_vars = ["DEPLOYANGEL_API_TOKEN", "DEPLOYANGEL_URL"]\n')
    assert (tmp_path / "AGENTS.md").read_text() == agent_instructions("deployangel")
    assert "`deployangel verify --commit=<sha> --wait --until=initial`" in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    for text in ("Created .mcp.json", "Created .cursor/mcp.json", "Created .codex/config.toml", "Created AGENTS.md",
                 "Created CLAUDE.md", "DEPLOYANGEL_API_TOKEN", "Never put it in these files"):
        assert text in out


def test_install_agents_runs_deployangel_through_uv_or_poetry(io_streams, tmp_path):
    (tmp_path / "uv.lock").write_text("")
    _install_agents(io_streams, tmp_path)
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["deployangel"] == {"command": "uv", "args": ["run", "deployangel", "mcp"]}
    assert 'args = ["run", "deployangel", "mcp"]' in (tmp_path / ".codex" / "config.toml").read_text()
    assert "`uv run deployangel verify --commit=<sha>" in (tmp_path / "AGENTS.md").read_text()

    poetry = tmp_path / "poetry"
    poetry.mkdir()
    (poetry / "poetry.lock").write_text("")
    _install_agents(io_streams, poetry)
    assert json.loads((poetry / ".mcp.json").read_text())["mcpServers"]["deployangel"]["command"] == "poetry"


def test_install_agents_adds_to_existing_files_and_changes_nothing_when_run_again(io_streams, tmp_path):
    from deployangel.cli import agent_instructions

    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}}}))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-5"\n')
    (tmp_path / "AGENTS.md").write_text("# Project\n\nRun the tests first.\n")
    (tmp_path / "CLAUDE.md").write_text("# Claude\n")

    _install_agents(io_streams, tmp_path)
    block = agent_instructions("deployangel")
    assert list(json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]) == ["github", "deployangel"]
    assert (tmp_path / ".codex" / "config.toml").read_text().startswith('model = "gpt-5"\n\n[mcp_servers.deployangel]\n')
    assert (tmp_path / "AGENTS.md").read_text() == "# Project\n\nRun the tests first.\n\n" + block
    assert (tmp_path / "CLAUDE.md").read_text() == "# Claude\n\n" + block

    paths = [".mcp.json", ".cursor/mcp.json", ".codex/config.toml", "AGENTS.md", "CLAUDE.md"]
    files = {path: (tmp_path / path).read_text() for path in paths}
    stdout, _ = io_streams
    stdout.truncate(0)
    stdout.seek(0)
    out = _install_agents(io_streams, tmp_path)
    assert {path: (tmp_path / path).read_text() for path in paths} == files
    assert "already has a deployangel MCP server" in out and "already has DeployAngel's instructions" in out


def test_install_agents_replaces_its_old_instructions_and_leaves_an_importing_claude_md_alone(io_streams, tmp_path):
    from deployangel.cli import AGENTS_END, AGENTS_START, agent_instructions

    (tmp_path / "AGENTS.md").write_text(f"# Project\n\n{AGENTS_START}\nold advice\n{AGENTS_END}\n\n## Style\n")
    (tmp_path / "CLAUDE.md").write_text("@AGENTS.md\n")
    out = _install_agents(io_streams, tmp_path)
    assert (tmp_path / "AGENTS.md").read_text() == "# Project\n\n" + agent_instructions("deployangel") + "\n## Style\n"
    assert (tmp_path / "CLAUDE.md").read_text() == "@AGENTS.md\n"
    assert "Updated DeployAngel's instructions in AGENTS.md." in out


def test_install_agents_leaves_a_config_it_cant_read(io_streams, tmp_path):
    (tmp_path / ".mcp.json").write_text("{ not json")
    out = _install_agents(io_streams, tmp_path)
    assert (tmp_path / ".mcp.json").read_text() == "{ not json"
    assert "Couldn't read .mcp.json, so it's unchanged." in out and 'command "deployangel", args ["mcp"]' in out


def test_reports_usage_and_auth_problems_with_exit_5(run, io_streams):
    assert run("verify", "--until=never", client=FakeClient()) == 5
    assert run("check", "--name=x", client=FakeClient()) == 5
    assert run("frobnicate", client=FakeClient()) == 5
    stdout, stderr = io_streams
    assert CLI(["verify"], stdout=stdout, stderr=stderr, env={}, git_head="abc1234").run() == 5
    assert "DEPLOYANGEL_API_TOKEN is not set" in stderr.getvalue()


class TestPlan:
    document = staticmethod(lambda: {**verdict_document("observing"), "exercise_plan": exercise_plan()})

    def test_says_whats_short_and_what_to_exercise(self, run):
        assert run("plan", "--format=text", client=FakeClient([self.document()])) == 0
        out = run.stdout.getvalue()
        for text in ("v184: Not cleared yet.", "requests: 12 of 30 (low-traffic rule)",
                     "routes run 3+ times: 1 of the 3 needed (3 normally active)",
                     "GET /orders/<int:pk>/ (normally active, run 1 of 3)",
                     "POST /password_resets/ (changed in this release, not run yet) [changes data]",
                     "send_invoice (normally active, runs when the app starts it)", "Use a test account, or ask first",
                     'Then report it: deployangel check --name="exercise plan"'):
            assert text in out
        # What clearance waits on comes first; the rest is only worth running.
        also = out.index("Also worth running, not needed to clear")
        assert out.index("Needed to clear") < out.index("send_invoice") < also < out.index("POST /password_resets/")

    def test_counts_normally_active_items_as_needed_from_a_server_that_doesnt_say(self, run):
        plan = exercise_plan()
        plan["items"] = [{k: v for k, v in item.items() if k != "needed"} for item in plan["items"]]
        run("plan", "--format=text", client=FakeClient([{**verdict_document("observing"), "exercise_plan": plan}]))
        out = run.stdout.getvalue()
        assert out.index("GET /orders/<int:pk>/") < out.index("Also worth running")

    def test_prints_the_deployment_and_plan_as_json(self, run):
        run("plan", "--format=json", client=FakeClient([self.document()]))
        data = json.loads(run.stdout.getvalue())
        assert list(data) == ["deployment", "exercise_plan"]
        assert data["exercise_plan"]["status"] == "exercisable"

    def test_says_plainly_when_the_server_returns_no_plan_or_nothing_is_found(self, run):
        run("plan", "--format=text", client=FakeClient([verdict_document("observing")]))
        assert "No exercise plan in this response; update the server." in run.stdout.getvalue()
        assert run("plan", client=FakeClient(deployments_list=[])) == 4

    def test_adds_the_first_items_to_verify_and_the_job_summary(self, run, tmp_path):
        path = tmp_path / "summary.md"
        run("verify", "--format=text", client=FakeClient([self.document()]), env={"GITHUB_STEP_SUMMARY": str(path)})
        assert "To clear sooner, exercise (deployangel plan for details):" in run.stdout.getvalue()
        assert "**To clear sooner, exercise (deployangel plan for details)**" in path.read_text()
        assert "- send_invoice" in path.read_text()
        assert "POST /password_resets/" not in run.stdout.getvalue() + path.read_text()

    def test_leaves_verify_alone_when_theres_nothing_to_exercise(self, run):
        warm = {**verdict_document("observing"), "exercise_plan": exercise_plan("warm_up")}
        run("verify", "--format=text", client=FakeClient([warm]))
        assert "To clear sooner" not in run.stdout.getvalue()


class TestCiEnvironment:
    def test_reads_github_actions(self):
        found = ci.detect({"GITHUB_ACTIONS": "true", "GITHUB_SHA": "abc1234def", "GITHUB_RUN_NUMBER": "123",
                           "GITHUB_RUN_ID": "9876", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "acme/shop"})
        assert found.to_dict() == {"provider": "github_actions", "commit": "abc1234def", "version": "run-123",
                                   "source_url": "https://github.com/acme/shop/actions/runs/9876"}

    def test_registers_kamals_release_linked_to_the_ci_run(self):
        assert ci.detect({"KAMAL_VERSION": "abc1234def", "KAMAL_COMMAND": "deploy"}).to_dict() == {
            "provider": "kamal", "commit": "abc1234def", "version": None, "source_url": None}
        in_ci = ci.detect({"KAMAL_VERSION": "2026.10.01", "GITHUB_ACTIONS": "true", "GITHUB_SHA": "abc1234def",
                           "GITHUB_RUN_ID": "9876", "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "acme/shop"})
        assert in_ci.to_dict() == {"provider": "kamal", "commit": None, "version": "2026.10.01",
                                   "source_url": "https://github.com/acme/shop/actions/runs/9876"}

    def test_recognizes_other_ci_and_nothing_outside_ci(self):
        assert ci.detect({"GITLAB_CI": "true", "CI_COMMIT_SHA": "a1", "CI_PIPELINE_IID": "45"}).version == "pipeline-45"
        assert ci.detect({"CIRCLECI": "true", "CIRCLE_SHA1": "b2", "CIRCLE_BUILD_NUM": "67"}).provider == "circleci"
        assert ci.detect({"BUILDKITE": "true", "BUILDKITE_COMMIT": "c3", "BUILDKITE_BUILD_URL": "http://insecure"}).source_url is None
        assert ci.detect({}) is None


class TestMcp:
    @pytest.fixture
    def server(self, clock):
        client = FakeClient([verdict_document("observing"), verdict_document("closed", "verified")])
        server = Server(client, output=io.StringIO(), error_output=io.StringIO(), sleeper=clock.advance, clock=clock,
                        git_head=lambda: "81ac27d0000")
        server.fake = client
        return server

    def request(self, server, method, params=None, id=1):
        return server.handle({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})

    def test_negotiates_the_protocol_version(self, server):
        result = self.request(server, "initialize", {"protocolVersion": "2025-03-26"})["result"]
        assert result["protocolVersion"] == "2025-03-26"
        assert result["serverInfo"]["name"] == "deployangel"
        assert "inconclusive means NOT verified" in result["instructions"]
        assert "get_exercise_plan" in result["instructions"] and "warm_up" in result["instructions"]
        assert self.request(server, "initialize", {"protocolVersion": "1999-01-01"})["result"]["protocolVersion"] == "2025-06-18"

    def test_lists_register_deployment_only_for_tokens_with_the_scope(self, server):
        names = lambda: [tool["name"] for tool in self.request(server, "tools/list")["result"]["tools"]]
        assert "register_deployment" not in names()
        server.fake.scopes = ["verifications:read", "deployments"]
        server._scopes = None
        assert {"register_deployment", "wait_for_verification", "get_exception", "get_exercise_plan"} <= set(names())

    def test_returns_every_tools_structured_content_as_an_object(self, server):
        server.fake.scopes = ["verifications:read", "deployments"]
        calls = {"get_verification": {}, "wait_for_verification": {"timeout_seconds": 1}, "get_exercise_plan": {},
                 "list_deployments": {}, "get_exception": {"fingerprint": "abc"}, "list_late_regressions": {},
                 "register_deployment": {"commit": "abc1234"}}
        assert sorted(tool["name"] for tool in self.request(server, "tools/list")["result"]["tools"]) == sorted(calls)
        for name, arguments in calls.items():
            result = self.request(server, "tools/call", {"name": name, "arguments": arguments})["result"]
            assert isinstance(result["structuredContent"], dict), f"{name} returned {type(result['structuredContent']).__name__}"
        listed = self.request(server, "tools/call", {"name": "list_deployments", "arguments": {}})["result"]["structuredContent"]
        assert listed == {"deployments": [{"id": 42}]}

    def test_waits_for_a_verdict_with_the_exit_codes_meaning(self, server):
        result = self.request(server, "tools/call", {"name": "wait_for_verification", "arguments": {"until": "verdict"}})["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["exit_code"] == 0
        assert result["structuredContent"]["meaning"] == "verified: the release is cleared"
        assert json.loads(result["content"][0]["text"])["verification"]["verification"]["verdict"] == "verified"
        assert server.fake.calls[0] == ("deployments", {"commit": "81ac27d0000", "version": None, "limit": 1})

    def test_returns_a_releases_exercise_plan(self, clock):
        client = FakeClient([{**verdict_document("observing"), "exercise_plan": exercise_plan()}])
        server = Server(client, output=io.StringIO(), error_output=io.StringIO(), git_head=lambda: "81ac27d0000")
        tools = self.request(server, "tools/list")["result"]["tools"]
        assert "test account or ask first" in next(t for t in tools if t["name"] == "get_exercise_plan")["description"]
        result = self.request(server, "tools/call", {"name": "get_exercise_plan", "arguments": {}})["result"]
        assert result["structuredContent"]["exercise_plan"]["status"] == "exercisable"
        assert result["structuredContent"]["deployment"]["version"] == "v184"

    def test_caps_each_wait_at_five_minutes(self, clock):
        stuck = Server(FakeClient([verdict_document("observing")]), output=io.StringIO(), error_output=io.StringIO(),
                       sleeper=clock.advance, clock=clock)
        result = stuck.handle({"id": 2, "method": "tools/call", "params": {
            "name": "wait_for_verification", "arguments": {"latest": True, "timeout_seconds": 9999}}})
        assert result["result"]["structuredContent"]["exit_code"] == 3
        assert result["result"]["structuredContent"]["in_progress"] is True
        assert clock.now == START + 300

    def test_ignores_notifications_and_rejects_unknown_methods_and_bad_json(self, server):
        assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
        assert self.request(server, "resources/list")["error"]["code"] == -32601
        assert server.handle_line("{nope")["error"]["code"] == -32700
        assert self.request(server, "tools/call", {"name": "rollback"})["error"]["code"] == -32602

    def test_speaks_one_json_message_per_line(self):
        input = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        output = io.StringIO()
        Server(FakeClient(), input=input, output=output, error_output=io.StringIO()).run()
        assert [json.loads(line) for line in output.getvalue().splitlines()] == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
