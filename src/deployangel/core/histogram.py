"""Sparse log-scale latency histogram, scheme log1.1_ms_v1 (Agent Protocol v1).
The agent only counts; percentiles are computed in the cloud after merging
every process, because percentiles cannot be averaged."""

from __future__ import annotations

import math

SCHEME = "log1.1_ms_v1"
LOG_GAMMA = math.log(1.1)
MAX_BUCKET = 400


def bucket_for(milliseconds: float) -> int:
    if milliseconds < 1:
        return 0
    return min(max(math.ceil(math.log(milliseconds) / LOG_GAMMA), 0), MAX_BUCKET)


class Histogram:
    __slots__ = ("counts",)

    def __init__(self) -> None:
        self.counts: dict[int, int] = {}

    def record(self, milliseconds: float) -> None:
        bucket = bucket_for(milliseconds)
        self.counts[bucket] = self.counts.get(bucket, 0) + 1

    def count(self) -> int:
        return sum(self.counts.values())

    def to_protocol(self) -> dict:
        return {"scheme": SCHEME, "counts": {str(bucket): self.counts[bucket] for bucket in sorted(self.counts)}}
