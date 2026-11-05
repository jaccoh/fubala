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

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        payload = json.dumps({"kind": kind, "data": data})
        for q in list(self._subscribers):
            q.put_nowait(payload)

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        self._subscribers.discard(q)


bus = EventBus()
