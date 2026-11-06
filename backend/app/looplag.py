"""Event-loop lag watchdog: logs when something blocks the loop.

A heartbeat task sleeps HEARTBEAT_S and measures how far past its deadline it
wakes. Anything above LOOP_LAG_WARN_S means sync work ran on the loop (the
23 Oct hang: sync Gitea calls in the scheduler tick)."""

from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

HEARTBEAT_S = 0.5
LOOP_LAG_WARN_S = 2.0


async def watch_loop_lag() -> None:
    while True:
        deadline = time.monotonic() + HEARTBEAT_S
        await asyncio.sleep(HEARTBEAT_S)
        lag = time.monotonic() - deadline
        if lag > LOOP_LAG_WARN_S:
            log.warning("event loop lag %.1fs", lag)
