# Changelog

## 0.1.10 (2026-10-09)

- Work scheduled outside the app's own scheduler is watched too.
  `recurring_jobs` declares it by the name it runs under (a task,
  `"manage.py <command>"`, or a `deployangel.task` name) with a cron line or
  words like `"every day at 4am"`, for cron, Heroku Scheduler, or a
  Kubernetes CronJob.
- `deployangel.task("name")`, as a `with` block or a decorator, records
  plain code that a scheduler starts as a run of that name, with its
  duration and whether it raised.
- Django management commands a schedule names are recorded with no code
  change.
- django-crontab's `CRONJOBS` setting is read: each job is expected on its
  schedule in the server's zone, and its runs are recorded.

## 0.1.9 (2026-10-09)

- `deployangel exercise` stops requesting a page after its first 404, 405,
  or 410, since a route table can list pages an app doesn't serve, and sends
  that page's share to the pages that answered.

## 0.1.8 (2026-10-09)

- `deployangel exercise --url=<production URL>` sends a release's exercise
  plan's read-only requests (GET routes without path parameters, at most
  200, about 5 a second) so a quiet release can clear sooner, then records
  what it sent on the release, which lists it as exercised from your side.
  It skips and names routes that change data or need a path parameter.
  `--dry-run` shows what it would send.
- The agent instructions `deployangel install agents` writes now tell the
  agent to run `deployangel exercise` when a release isn't cleared yet.

## 0.1.7 (2026-10-09)

- `deployangel install agents` sets up Claude Code, Cursor, and Codex in a
  project: the MCP server in `.mcp.json`, `.cursor/mcp.json`, and
  `.codex/config.toml` (forwarding `DEPLOYANGEL_API_TOKEN`), and instructions
  to wait for a verdict after deploying in `AGENTS.md`, with a `CLAUDE.md`
  that imports it. It runs through `uv run` or `poetry run` in projects that
  use them, never replaces existing entries, and re-running it only updates
  its own instructions.

## 0.1.6 (2026-10-08)

- The agent gets its file digests when it starts, so the reporter works out
  the code fingerprint right away rather than at its first send, a minute or
  two later. A file changed in between, as by an in-place `git pull`, no
  longer ends up in the fingerprint of code that isn't running.

## 0.1.5 (2026-10-08)

- The agent finds the release in two more places, so fewer apps report it as
  unknown. After a `REVISION` file, it reads the commit from a git checkout
  (source `git_head`): `.git` in the app's root or up to 3 directories above
  it, following worktrees and packed refs, without running `git`. After ECS,
  it falls back to a code fingerprint (source `code_fingerprint`): `code:`
  and the first 12 characters of the hash of the file digests it already
  sends, computed in the reporter thread as soon as it starts rather than at
  boot. It logs a warning suggesting `DEPLOYANGEL_REVISION`: without a commit
  DeployAngel shows which files changed in a release, but not its commits and
  pull requests. With file digests off the release stays unknown.
- `deployangel install docker` bakes the commit into a Docker image: it adds
  `ARG GIT_SHA` and `ENV DEPLOYANGEL_REVISION=$GIT_SHA` at the end of the
  Dockerfile's last stage, before `CMD` and `ENTRYPOINT`, so earlier layers
  stay cached, and prints the `--build-arg` to pass. It never edits CI
  workflows.

## 0.1.4 (2026-10-06)

- Checkpoints report whether they were recorded while handling an HTTP request
  or while running a job (each entry in `checkpoints` gains `http` and `job`
  counts), so DeployAngel compares a checkpoint recorded in jobs against job
  traffic rather than requests. A task run eagerly inside a request counts as
  a job. This needs a DeployAngel server that reads the new fields; older
  servers ignore them.

## 0.1.3 (2026-10-06)

- `deployangel plan` separates what clearance waits on ("Needed to clear")
  from changed and rarely used paths that are only worth running, and
  `deployangel verify` and the GitHub Actions job summary list only what's
  needed. The MCP tool `get_exercise_plan` describes the server's new
  `needed` flag on each item.

## 0.1.2 (2026-10-06)

- The MCP tools `list_deployments` and `list_late_regressions` return their
  lists inside an object (`{"deployments": [...]}`, `{"late_regressions":
  [...]}`). MCP requires a tool's structured content to be an object, so
  clients such as Claude Code rejected the bare lists before the agent saw
  them.

## 0.1.1 (2026-10-06)

- The package includes the `deployangel` command and MCP server, so Python
  projects no longer need the Ruby gem for them: `verify`, `status`, `plan`,
  `release`, `check`, `exception`, `install kamal`, and `mcp`, with the same
  options, output, and exit codes as the Ruby gem's command. `uvx deployangel
  mcp` runs the MCP server without installing anything. Standard library
  only, like the agent; the command never starts the agent.

## 0.1.0 (2026-10-06)

- First release: the DeployAngel agent for Python, speaking Agent Protocol v1.
- Django 4.2+ (WSGI and ASGI), FastAPI 0.100+, Starlette 0.27+, and Flask 2.3+:
  requests by matched route pattern, status codes, latency histograms, and
  unhandled exceptions; the route table with each view's file.
- Celery 5.3+: every task attempt, retries and failures, queue latency, task
  files, and Celery Beat schedules, from settings and from django-celery-beat.
- RQ 1.16+: `deployangel.rq.Worker` and `SimpleWorker` record every job,
  including failures from RQ's forked work horses.
- Checkpoints (`deployangel.checkpoint`), handled exceptions
  (`deployangel.notify`), critical flows, ignored routes, and turning
  exception messages off.
- Release identity from the same sources as the Ruby agent: configuration,
  Heroku dyno metadata, Kamal, Render, Fly.io, Railway, Coolify, Dokku, a
  `REVISION` file, and ECS.
