"""Asyncio in-memory repeater state for the WebSocket repeater.

Stores a single slot value with a monotonic revision counter and a set
of subscriber queues. Every ``write`` pushes an ``event`` frame to every
subscriber's ``asyncio.Queue`` (dropping on full queues so slow
subscribers never block the writer).

Token validation is synchronous (``hmac.compare_digest``) so the server
handler can validate the initial ``auth`` frame before subscribing.

This module uses only the standard library (``asyncio``, ``hmac``).
"""
from __future__ import annotations

import asyncio
import hmac

__all__ = ["WsRepeaterState"]


class WsRepeaterState:
    """In-memory slot + revision + subscriber set for the WS repeater.

    Parameters
    ----------
    tokens:
        When ``None`` (default), any token is accepted.  When a set/list
        of strings is given, only those exact tokens pass validation
        (compared with :func:`hmac.compare_digest`).  An empty collection
        refuses all tokens.
    maxsize:
        Maximum items per subscriber queue.  Full queues silently drop
        new events so a slow subscriber never blocks the writer.
    """

    def __init__(
        self,
        *,
        tokens: set[str] | list[str] | dict[str, str] | None = None,
        maxsize: int = 100,
    ) -> None:
        self._slot_value: str = ""
        self._revision: int = 0
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()
        if isinstance(tokens, dict):
            self._token_to_name: dict[str, str] | None = dict(tokens)
            self._tokens: set[str] | None = set(tokens.keys())
        elif tokens is not None:
            self._token_to_name = None
            self._tokens = set(tokens)
        else:
            self._token_to_name = None
            self._tokens = None
        self._maxsize: int = maxsize

    async def write(self, value: str) -> int:
        """Store *value*, bump revision, push event to all subscribers.

        Returns the new revision.
        """
        async with self._lock:
            self._slot_value = value
            self._revision += 1
            rev = self._revision
            event = {"type": "event", "value": value, "revision": rev}
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass  # drop event for slow subscriber
        return rev

    async def snapshot(self) -> tuple[str, int]:
        """Return ``(value, revision)`` under lock."""
        async with self._lock:
            return (self._slot_value, self._revision)

    async def add_subscriber(self) -> asyncio.Queue:
        """Create a subscriber queue, register it, and return it."""
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def remove_subscriber(self, q: asyncio.Queue) -> None:
        """Discard *q* from the subscriber set."""
        async with self._lock:
            self._subscribers.discard(q)

    def validate_token(self, token: str) -> str | bool | None:
        """Return the token name if accepted, ``True`` if accepted (no name mapping), or ``None`` if rejected.

        ``None`` tokens → accept all (returns ``True``).  Empty set → refuse all.
        Otherwise, compare with :func:`hmac.compare_digest`.
        """
        if self._tokens is None:
            return True
        if self._token_to_name is not None:
            for t, name in self._token_to_name.items():
                if hmac.compare_digest(token, t):
                    return name
            return None
        return True if any(hmac.compare_digest(token, t) for t in self._tokens) else None