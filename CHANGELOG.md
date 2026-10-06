# Changelog

## Unreleased

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
