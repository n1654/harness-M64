"""In-process event bus — single producer (the agent) -> many consumers (monitor, log)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict


@dataclass
class Event:
    """A bus event. `kind` drives routing; `data` carries payload."""

    kind: str
    data: Dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


Subscriber = Callable[[Event], Awaitable[None]]


class EventBus:
    """Async pub/sub. Subscribers are coroutines invoked sequentially per event.

    Notes:
        * Single-process only by design; cross-process variant lives behind the
          same interface and can be swapped without touching producers.
        * Subscribers must not block: a slow subscriber slows the whole bus.
    """

    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = asyncio.Lock()

    async def subscribe(self, cb: Subscriber) -> None:
        async with self._lock:
            self._subscribers.append(cb)

    async def publish(self, kind: str, **data: Any) -> None:
        evt = Event(kind=kind, data=data)
        async with self._lock:
            subs = list(self._subscribers)
        for cb in subs:
            try:
                await cb(evt)
            except Exception:  # noqa: BLE001
                # A subscriber's failure must not kill the bus.
                import logging

                logging.getLogger("harness.bus").warning(
                    "subscriber failed for kind=%s", kind, exc_info=True
                )
