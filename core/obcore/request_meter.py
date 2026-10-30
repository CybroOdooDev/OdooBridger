"""Requests answered and failed (5xx) by this process over the last 24 hours.

Kept in memory per process, by minute, so it costs nothing per request. A
restart starts the count again; the staff dashboard says so (``started_at``).

Not counted: the monitoring polls themselves (``/health`` and the metrics
read), which would otherwise be most of the traffic on a quiet install.
Counted apart: 401/403 answers (``auth_failed``). They are not server
failures, but a wave of them right after a deploy is a broken sign-in.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone

WINDOW_MINUTES = 24 * 60

_lock = threading.Lock()
_buckets: deque[list[int]] = deque()   # [minute, total, failed, auth_failed]
_started = time.time()

#: Paths that are the platform watching the service, not the service being used.
MONITORING_PATHS = ("/health", "/platform/v1/metrics")


def note(status_code: int, path: str | None = None) -> None:
    if path is not None and path.rstrip("/") in MONITORING_PATHS:
        return
    minute = int(time.time() // 60)
    with _lock:
        if not _buckets or _buckets[-1][0] != minute:
            _buckets.append([minute, 0, 0, 0])
            while _buckets and _buckets[0][0] <= minute - WINDOW_MINUTES:
                _buckets.popleft()
        _buckets[-1][1] += 1
        if status_code >= 500:
            _buckets[-1][2] += 1
        elif status_code in (401, 403):
            _buckets[-1][3] += 1


def summary() -> dict:
    floor = int(time.time() // 60) - WINDOW_MINUTES
    with _lock:
        total = sum(b[1] for b in _buckets if b[0] > floor)
        failed = sum(b[2] for b in _buckets if b[0] > floor)
        auth = sum(b[3] for b in _buckets if b[0] > floor)
    return {"window_hours": 24, "total": total, "failed": failed, "auth_failed": auth,
            "since_minutes": min(WINDOW_MINUTES, int((time.time() - _started) // 60)),
            "started_at": datetime.fromtimestamp(_started, timezone.utc).isoformat(),
            "scope": "this process"}


def reset() -> None:
    with _lock:
        _buckets.clear()
