# Changelog

## Unreleased

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
