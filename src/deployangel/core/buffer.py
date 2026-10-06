"""Bounded FIFO of payloads waiting to be sent. When full, the oldest payload
is dropped: losing telemetry is always preferable to growing memory in the
customer's process."""

from __future__ import annotations

import threading
from collections import deque


class Buffer:
    def __init__(self, limit: int):
        self.limit = limit
        self.dropped = 0
        self._items: deque = deque()
        self._lock = threading.Lock()

    def push(self, item) -> None:
        with self._lock:
            self._items.append(item)
            while len(self._items) > self.limit:
                self._items.popleft()
                self.dropped += 1

    def shift(self):
        with self._lock:
            return self._items.popleft() if self._items else None

    def unshift(self, item) -> None:
        with self._lock:
            if len(self._items) < self.limit:
                self._items.appendleft(item)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
