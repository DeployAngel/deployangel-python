# DeployAngel agent for Python: working rules

The `deployangel` package runs inside customers' production Python apps.
These rules come before any feature.

## Never harm the customer's app

- No DeployAngel network calls during a request or job. Payloads go out from a
  background thread, once a minute, with short timeouts.
- Fail open: every public function catches its own errors. Nothing may raise
  into, or block, the app.
- Keep buffers bounded, and drop telemetry rather than block or fail.
- Keep overhead very low: around 1% CPU or less, where practical
  (`python bench/overhead.py`).
- If DeployAngel is down, the customer's app must not notice.
- No runtime dependencies: the standard library only. Framework imports stay
  inside their integration module.

## Send aggregates, never events

- One payload per process per minute, whatever the traffic.
- Only mergeable values: counts and histograms. Never percentiles or rates;
  the cloud computes those after merging processes.
- Bound every list in a payload (routes, exceptions, jobs, checkpoints).

## Send as little as possible

Never collect request bodies, parameters, cookies, authorization headers,
session contents, SQL parameters, email addresses, other personal data, or raw
logs. Record route patterns (`/users/<int:pk>/`), never raw paths, and
sanitize exception messages. If a change would add a line to the README's
"What it sends" section, that's a decision for the maintainer, not an
implementation detail.

## Keep the protocol framework-neutral

The Agent Protocol speaks in HTTP, routes, exceptions, jobs, queues, and
scheduled tasks, never in Django views, Celery tasks, or Flask blueprints. The
integrations (`deployangel/django/`, `asgi.py`, `flask.py`, `celery.py`,
`rq.py`) translate their framework into those concepts, and the Ruby agent
speaks the same protocol. Protocol changes must work for both, and the
DeployAngel cloud has to accept them first.

## Compatibility and tests

- Supports Python 3.10, Django 4.2, FastAPI 0.100, Starlette 0.27, Flask 2.3,
  Celery 5.3, and RQ 1.16, or later. CI runs the oldest and newest of each
  (`.github/workflows/ci.yml`), so don't use newer APIs without a fallback.
- `pytest` must pass. Test the failure paths too: network errors, timeouts,
  full buffers, and forks.
- Update the README and CHANGELOG when what the agent sends, or how it's
  configured, changes.
