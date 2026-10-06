"""One OS process. The random suffix is regenerated after fork, so a forked
child never reuses its parent's identity."""

from __future__ import annotations

import os
import secrets
import socket
from datetime import datetime, timezone
from typing import Mapping


class Instance:
    def __init__(self, env: Mapping[str, str] = os.environ, now: float = 0.0):
        dyno = env.get("DYNO")
        self.host = dyno or socket.gethostname()
        self.pid = os.getpid()
        self.process_type = dyno.split(".")[0] if dyno else None
        self.id = f"{self.host}:{self.pid}:{secrets.token_hex(3)}"
        self.started_at = now

    def to_protocol(self) -> dict:
        data = {
            "id": self.id,
            "host": self.host,
            "pid": self.pid,
            "process_type": self.process_type,
            "started_at": iso8601(self.started_at),
        }
        return {key: value for key, value in data.items() if value is not None}


def iso8601(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
