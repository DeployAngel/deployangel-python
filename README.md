# DeployAngel for Python

The DeployAngel agent for Django, FastAPI, Starlette, and Flask, with Celery
and RQ jobs. It watches what your app does in production and reports one
small aggregated payload per process per minute. DeployAngel uses that to
verify every deployment, and to tell you (or your coding agent) when a release
is **cleared** and you can stop watching it.

## Install

Requires Python 3.10 or later. The package has no dependencies of its own.

```bash
pip install deployangel
```

Set this in production, for every process (web servers and job workers alike):

```bash
DEPLOYANGEL_TOKEN=da_live_...           # an ingestion token with the telemetry scope
```

On Heroku, the DeployAngel add-on sets it for you.

### Django

Supports Django 4.2 or later.

```python
# settings.py
INSTALLED_APPS = [
    # ...
    "deployangel.django",
]
```

That's all. The app puts DeployAngel's middleware at the top of `MIDDLEWARE`
(list it yourself to place it), records requests under WSGI and ASGI, and
instruments Celery when it's installed. Reporting is on when `DEBUG` is off.

### FastAPI and Starlette

Supports FastAPI 0.100 and Starlette 0.27 or later.

```python
import deployangel.fastapi

app = FastAPI()
deployangel.fastapi.init(app)      # deployangel.starlette.init(app) for Starlette
```

Call it where the app is created, before it serves requests.

### Flask

Supports Flask 2.3 or later.

```python
import deployangel.flask

app = Flask(__name__)
deployangel.flask.init_app(app)
```

### Celery

Supports Celery 5.3 or later. In Django it's automatic. Elsewhere, call
`init` where the Celery app is created, so worker processes start the agent:

```python
import deployangel.celery

app = Celery("shop")
deployangel.celery.init(app)
```

### RQ

Supports RQ 1.16 or later. Run workers with DeployAngel's worker class:

```bash
rq worker -w deployangel.rq.Worker               # or deployangel.rq.SimpleWorker
python manage.py rqworker --worker-class deployangel.rq.Worker   # django-rq
```

RQ runs each job in a forked process that exits as soon as the job ends, so
the worker records each job after it finishes, from what RQ saved in Redis.
That includes a failed job's traceback, but not the exception of an attempt RQ
retries.

### Which release is running

The agent must know which release it's running. It finds it in this order:

1. `DEPLOYANGEL_REVISION` (the commit SHA) and `DEPLOYANGEL_RELEASE_VERSION`, if you set them
2. Heroku dyno metadata. Enable it with `heroku labs:enable runtime-dyno-metadata`
   and `heroku labs:enable runtime-dyno-build-metadata`; both take effect on the
   next deploy
3. Kamal: `KAMAL_VERSION`
4. Render: `RENDER_GIT_COMMIT`
5. Fly.io: the deploy's image tag, from `FLY_IMAGE_REF`. Fly.io sets no commit, so
   pass one in for commit-level change tracking (`ENV DEPLOYANGEL_REVISION=$GIT_SHA`
   in the Dockerfile, with `--build-arg GIT_SHA=$(git rev-parse HEAD)`)
6. Railway: `RAILWAY_GIT_COMMIT_SHA`, or `RAILWAY_DEPLOYMENT_ID`
7. Coolify: `SOURCE_COMMIT`. Dokku: `GIT_REV`
8. a `REVISION` file in the app's root
9. a git checkout: the commit `HEAD` names, read from `.git` in the app's root
   or up to 3 directories above it, without running `git`. This covers servers
   deployed by `git pull`, Fabric, or Ansible
10. ECS, including Fargate: the container's image, from the metadata endpoint ECS
    provides (one local request at boot)
11. a code fingerprint: `code:` and the first 12 characters of the hash of the
    file digests the agent already sends (below), with no commit. The same
    code gives the same fingerprint, so a redeploy of unchanged code isn't a
    new release. It's computed in the background when the reporter starts, not
    at boot, and needs file digests on. Without a commit DeployAngel still
    shows which files changed in a release, but not its commits and pull
    requests, so the agent logs a warning: set `DEPLOYANGEL_REVISION` when
    you can

For other Docker deploys, bake the commit into the image:
`deployangel install docker` adds `ARG GIT_SHA` and
`ENV DEPLOYANGEL_REVISION=$GIT_SHA` to the end of your Dockerfile, where it
doesn't invalidate cached layers, and you build with
`--build-arg GIT_SHA=$(git rev-parse HEAD)`. On DigitalOcean App Platform, set
`DEPLOYANGEL_REVISION: ${_self.COMMIT_HASH}` in the app spec.

## What it sends

- HTTP request counts, 4xx/5xx counts by status code, unhandled exceptions,
  and a latency histogram, both per app and per route.
- Routes are recorded as the pattern the framework matched
  (`GET /users/<int:pk>/`, `GET /items/{item_id}`), never the raw path. At
  most 100 routes are sent per payload; the rest are folded into `__other__`.
- Background jobs: attempts, failed attempts, discarded jobs, duration, and
  queue latency, per task. For queue latency, Celery task messages carry one
  extra header, `deployangel_published_at`, the time they were sent.
- Release identity, runtime versions, and a per-process instance ID.
- Exceptions: a stable fingerprint, the exception class, the first line of the
  message, and application frames only (file paths and function names). In
  the message, numbers, IDs, emails, UUIDs, long hex strings, quoted values,
  and the request's host are replaced with placeholders. Other words are kept:
  `Payment failed for Jane Doe` is sent as it is. If your app's messages might
  hold personal or health data, [turn messages off](#exception-messages).
- Once per process: the route table (with each view's module and file), task
  names and files, recurring schedules declared for Celery Beat (in settings
  or in django-celery-beat's tables), critical flows, and file digests
  (relative paths and hashes, never file contents) so DeployAngel can tell
  which routes changed in a release. Disable digests with
  `DEPLOYANGEL_FILE_DIGESTS=false`.

It does not send request bodies, parameters, headers, cookies, SQL, logs, or
user data.

## Data and pricing

- Everything goes to DeployAngel's hosted service, which runs on DigitalOcean
  in the United States. There's no self-hosted version, and no choice of
  region.
- Telemetry is kept for 21 days, and release history for 7, 30, or 90 days
  depending on the plan. The [privacy policy](https://deployangel.com/privacy)
  has the details and the services DeployAngel uses.
- DeployAngel is free during the beta, with notice before paid plans start.
  See [pricing](https://deployangel.com/pricing).

## Safety

- Nothing runs on the network during a request or job. Recording only updates
  in-memory counters.
- Payloads are sent from a background thread, once a minute, with short
  timeouts.
- When DeployAngel is unreachable, the buffer is bounded (10 payloads, kept as
  gzipped JSON) and the oldest are dropped. Your app is never blocked or failed.
- Safe across forks (Gunicorn, Celery's prefork pool, uWSGI), and the minute
  in progress is flushed at shutdown. Under uWSGI, enable threads
  (`enable-threads = true`).

`python bench/overhead.py` measures this. On an Apple M-series laptop with
Python 3.14, recording a request adds about 1.7 µs, and with every route, job,
checkpoint, and exception list at its cap the agent holds about 0.5 MB,
including 10 unsent minutes.

## Configuration

Environment variables are enough for most apps. In Django, settings go in a
`DEPLOYANGEL` dict; elsewhere, call `deployangel.configure` before `init`:

```python
# settings.py
DEPLOYANGEL = {
    "environments": ["production", "staging"],   # default: production only
}

# or anywhere else
deployangel.configure(environments=["production", "staging"])
```

The agent reports in the environments listed. Python frameworks don't name an
environment, so it's `DEPLOYANGEL_ENVIRONMENT` if set, otherwise
`development` when the framework's debug mode is on and `production` when
it's off. `DEPLOYANGEL_ENABLED=true|false` forces reporting on or off
anywhere. `DEPLOYANGEL_URL` overrides the API endpoint (default
`https://api.deployangel.com`).

File paths are relative to the app's root, the working directory by default
(on Heroku and in most containers, the repository's root). Set
`DEPLOYANGEL_ROOT` if your processes start somewhere else.

Critical flows (for example sign-up or password reset) are always listed in
clearance reports:

```python
DEPLOYANGEL = {
    "critical_flows": {"password_reset": ["POST /password-reset/", "job:accounts.tasks.send_reset_email"]},
}
```

Health checks aren't recorded: load balancers and uptime monitors call them
all the time and they always answer fast, so they would make your app look
busier and healthier than its real pages. The agent recognizes
django-health-check, django-alive, and django-watchman wherever they're
mounted, and any route at a conventional path (`/up`, `/health`, `/healthz`,
`/healthcheck`, `/health_check`, `/livez`, `/readyz`, `/statusz`, `/ping`,
`/ht`, `/alive`). If yours is somewhere else, list it the way the dashboard
shows it. HEAD requests to it are left out too:

```python
DEPLOYANGEL = {"ignored_routes": ["GET /status/"]}
```

### Exception messages

To send exceptions without any message, only their class, fingerprint, and
application frames:

```python
DEPLOYANGEL = {"exception_messages": False}   # or DEPLOYANGEL_EXCEPTION_MESSAGES=false
```

Grouping, new-exception detection, and verdicts work the same, since the
fingerprint never uses the message. You lose the message text in the
dashboard, notifications, and AI investigation.

### Handled exceptions

Exceptions your code catches aren't seen. To report one for context (handled
exceptions never fail a release):

```python
try:
    sync_inventory()
except UpstreamError as error:
    deployangel.notify(error)
```

### Recurring jobs

DeployAngel expects declared recurring tasks on schedule. It reads Celery
Beat's `beat_schedule` (crontabs, and intervals, which repeat from when Beat
starts) in the zone Celery uses, and, when `django_celery_beat` is installed,
its enabled periodic tasks. Solar schedules aren't read, and neither are
RQ's schedulers, whose jobs live only in Redis. DeployAngel also learns
recurring jobs from their history.

## Checkpoints

Errors and latency don't catch work that silently stops happening. Count the
business events that matter with one line:

```python
deployangel.checkpoint("order.created")
deployangel.checkpoint("webhook.stripe.processed", count=len(events))
```

DeployAngel learns each checkpoint's normal rate relative to your traffic and
fails a release after which it drops sharply or stops, even when every request
and job still succeeds. Only drops are flagged, and a checkpoint without enough
traffic never blocks a release from being cleared. Checkpoints can also be part
of a critical flow (`checkpoint:order.created`).

The agent also records whether each checkpoint happened while handling an HTTP
request or while running a job (a Celery task or RQ job, including one run
eagerly inside a request), so DeployAngel compares it against the right
traffic: requests for checkpoints recorded in requests, job runs for ones
recorded in jobs.

It's safe to call anywhere: it never raises, never touches the network, and is
ignored outside reporting environments. Names use letters, numbers, and
`. _ : -` (up to 100 characters); keep them to a fixed set rather than
including IDs, since only 100 distinct names are counted per minute.

## Registering deploys

DeployAngel notices a new release when the agent first reports it, and
verifies it from there, with nothing to set up. On Heroku, the add-on also
registers every release for you.

To register deploys from CI, which also catches a release that never boots,
use an API token created for **CI deploys** in the dashboard. On GitHub
Actions, add [DeployAngel/verify-release](https://github.com/DeployAngel/verify-release)
after your deploy step: it registers the deploy, waits for the verdict, and
fails the step if the release fails.

```yaml
- uses: DeployAngel/verify-release@v1
  with:
    api-token: ${{ secrets.DEPLOYANGEL_API_TOKEN }}
```

Anywhere else, post the commit:

```bash
curl -fsS https://api.deployangel.com/api/v1/deployments \
  -H "Authorization: Bearer $DEPLOYANGEL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"commit\": \"$GIT_SHA\"}"
```

## Command line and coding agents

The package includes the `deployangel` command (also `python -m deployangel`)
and an MCP server for coding agents. They talk only to DeployAngel's API:
they never start the agent or load your app, so they run anywhere the package
is installed, or with nothing installed through `uvx deployangel`. Give them a
token in `DEPLOYANGEL_API_TOKEN`: a "CLI & coding agents" token reads
verdicts, and a "CI deploys" token can also register deploys and report
checks.

```bash
deployangel verify --wait                   # current git HEAD, until a verdict
deployangel verify --wait --until=initial   # return at the 15-minute initial check
deployangel status                          # latest deployment
deployangel plan                            # what to exercise so a release clears sooner
deployangel exercise --url=https://example.com  # send the plan's read-only requests to production
deployangel release --commit=$SHA           # register a deploy (manual or CI)
deployangel check --name="smoke: signup" --status=pass --covers=registration
deployangel install kamal                   # a Kamal post-deploy hook that registers each deploy
deployangel install docker                  # bake the commit into the image as DEPLOYANGEL_REVISION
deployangel install agents                  # set up Claude Code, Cursor, and Codex in this project
```

Output is text on a terminal and JSON when piped (`--format=text|json`). Exit
codes: 0 cleared, 1 failed, 2 not cleared, 3 still verifying or timed out, 4
deployment not found, 5 usage, auth, or network error, 6 no problems so far
at the initial check (not cleared), 7 warnings at the initial check. In
GitHub Actions, `verify` also writes the verdict to the job's summary.

For coding agents, run `deployangel install agents` in the project. It adds
the MCP server to `.mcp.json` (Claude Code), `.cursor/mcp.json` (Cursor), and
`.codex/config.toml` (Codex, which reads it only in projects you trust), and
instructions to wait for a verdict after deploying to `AGENTS.md`, with a
`CLAUDE.md` that imports it. In a uv or Poetry project it runs `deployangel`
through `uv run` or `poetry run`. It never replaces an existing entry, and
running it again updates only its own instructions. Commit the files; the
token stays in your environment.

Or add the server yourself:

```bash
claude mcp add deployangel -- deployangel mcp        # Claude Code (or: -- uvx deployangel mcp)
```

For Codex, in `~/.codex/config.toml`:

```toml
[mcp_servers.deployangel]
command = "deployangel"     # or "uvx", with args = ["deployangel", "mcp"]
args = ["mcp"]
```

Any other MCP client (Cursor, VS Code, Zed, ...) runs the same command. The
server needs `DEPLOYANGEL_API_TOKEN` in the environment it starts in.

The tools are `get_verification`, `wait_for_verification`,
`get_exercise_plan`, `list_deployments`, `get_exception`,
`list_late_regressions`, and `register_deployment` when the token allows it.
None of them can change production. When a release isn't cleared yet,
`deployangel plan` (or `get_exercise_plan`) says what stands between it and
clearance, and what to exercise against production so it clears sooner.
It separates what clearance waits on from changed and rarely used paths
that are only worth running. Routes that change data are marked; use a test account for them, or ask
first. `deployangel exercise --url=<production URL>` does the read-only part:
it requests the plan's GET routes that have no path parameters, spreading any
request shortfall across them (at most 200 requests, about 5 a second, as
`DeployAngel-Exercise`), then records what it sent on the release, where the
page lists it under "Exercised from your side". It skips and names routes
that change data or need a path parameter, and stops requesting a page after
its first 404, sending its share to the pages that answered. `--dry-run`
shows what it would send.

The command and its output match the Ruby gem's `deployangel` command, so the
docs and agent instructions for either apply to both.

## Development

```bash
python -m venv .venv
.venv/bin/pip install -e . django djangorestframework django-celery-beat fastapi httpx flask celery rq fakeredis pytest pytest-asyncio
.venv/bin/pytest
```

`RQ_REDIS_URL=redis://localhost:6379/15` also runs RQ's forking worker
against a real Redis.

The agent speaks DeployAngel Agent Protocol v1: one gzipped JSON payload per
process per minute to `POST /api/v1/telemetry`, and the application's metadata
once per process to `POST /api/v1/application_metadata`.
`src/deployangel/core/protocol.py` and `src/deployangel/metadata.py` build
them.
