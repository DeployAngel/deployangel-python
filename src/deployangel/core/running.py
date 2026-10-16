"""The jobs this process is running now, so each payload can say how long the
oldest of each class has been running: a job that hangs never finishes, so it
never shows up in the counts. Starting and finishing is a dict update under a
lock."""

from __future__ import annotations

import threading
from typing import Callable, Optional

# More than any process runs at once; past it, new jobs go untracked rather
# than growing memory.
MAX = 1000


class RunningJobs:
    def __init__(self, clock: Callable[[], float]):
        self._clock = clock
        self._jobs: dict = {}
        self._lock = threading.Lock()

    def start(self, job_class: str) -> object:
        """Returns the token to pass to finish()."""
        token = object()
        with self._lock:
            if len(self._jobs) < MAX:
                self._jobs[token] = (str(job_class), self._clock())
        return token

    def finish(self, token: Optional[object]) -> None:
        with self._lock:
            self._jobs.pop(token, None)

    def snapshot(self, at: float) -> dict:
        """{job_class: (running, oldest_running_ms)} for jobs that started by
        at and are still running."""
        running: dict = {}
        with self._lock:
            for job_class, started_at in self._jobs.values():
                if started_at > at:
                    continue
                age_ms = int(round((at - started_at) * 1000))
                count, oldest = running.get(job_class, (0, 0))
                running[job_class] = (count + 1, max(oldest, age_ms))
        return running
