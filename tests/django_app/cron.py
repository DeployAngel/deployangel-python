"""Functions django-crontab runs from the CRONJOBS setting in the tests."""

ran = []


def nightly_cleanup(limit=10):
    ran.append(limit)
    return limit


def failing_job():
    raise RuntimeError("cron job broke")
