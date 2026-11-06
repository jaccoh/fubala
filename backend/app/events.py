"""In-process pub/sub for live board updates (SSE substrate).

The bus fans story events out to subscribed SSE streams; the same events are
persisted as StoryEvent rows for history. Phase 3's MCP wait_for_story will
block on this bus too.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        # the loop each queue belongs to (asyncio.Queue is not thread-safe)
        self._loops: dict[asyncio.Queue[str], asyncio.AbstractEventLoop] = {}

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Fan out to subscribers. Safe from any thread: off a queue's loop
        thread the put is forwarded with call_soon_threadsafe."""
        payload = json.dumps({"kind": kind, "data": data})
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        for q in list(self._subscribers):
            loop = self._loops.get(q)
            if loop is None or loop is current or loop.is_closed():
                q.put_nowait(payload)
            else:
                loop.call_soon_threadsafe(q.put_nowait, payload)

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._loops[q] = asyncio.get_running_loop()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)
        self._loops.pop(q, None)


bus = EventBus()
