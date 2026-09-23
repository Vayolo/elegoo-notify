"""Event bus asincrono pub/sub interno.

Ogni evento è un dict {"type": str, "data": dict|None, "ts": float}.
I subscriber con coda piena perdono gli eventi più vecchi (drop-oldest)
per non bloccare mai il publisher.
"""
from __future__ import annotations

import asyncio
import fnmatch
import time
from typing import Any, Callable


class Subscription:
    """Sottoscrizione al bus: iterabile async e con callback opzionale."""

    def __init__(self, bus: "EventBus", patterns: tuple[str, ...], queue_size: int = 300):
        self.bus = bus
        self.patterns = patterns  # () => tutti gli eventi
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._callback: Callable[[dict], Any] | None = None

    def matches(self, event_type: str) -> bool:
        if not self.patterns:
            return True
        return any(fnmatch.fnmatchcase(event_type, p) for p in self.patterns)

    def set_callback(self, cb: Callable[[dict], Any]) -> None:
        self._callback = cb

    def _offer(self, event: dict) -> None:
        if self._callback is not None:
            try:
                res = self._callback(event)
                if asyncio.iscoroutine(res):
                    asyncio.create_task(res)  # callback async fire-and-forget
            except Exception:  # pragma: no cover
                import logging
                logging.getLogger("elegoo.events").exception("callback subscriber in errore")
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()  # drop-oldest
                self.queue.put_nowait(event)
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self.bus.unsubscribe(self)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        return await self.queue.get()

    async def next_event(self, pattern: str = "*", timeout: float = 30.0) -> dict:
        """Attende il prossimo evento che matcha pattern (o raise TimeoutError)."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Nessun evento '{pattern}' entro {timeout}s")
            try:
                event = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if fnmatch.fnmatchcase(event.get("type", ""), pattern):
                return event


class EventBus:
    def __init__(self) -> None:
        self._subs: list[Subscription] = []

    def subscribe(self, *patterns: str, queue_size: int = 300) -> Subscription:
        sub = Subscription(self, patterns, queue_size)
        self._subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        if sub in self._subs:
            self._subs.remove(sub)

    def publish(self, event_type: str, data: Any = None) -> None:
        event = {"type": event_type, "data": data, "ts": time.time()}
        for sub in list(self._subs):
            if sub.matches(event_type):
                sub._offer(event)

    def close_all(self) -> None:
        for s in list(self._subs):
            s.close()
