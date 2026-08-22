"""Repeater in-memory state — slot value, revision, SSE subscribers.

Pure state management with no HTTP coupling.  Thread-safe via a single
:class:`threading.Lock`.  Used by :mod:`cliptunnel_mcp.repeater.server`.

Stdlib only — no third-party dependencies.
"""
from __future__ import annotations

import hmac
import json
import queue
import threading

__all__ = ["RepeaterState"]

# Default max queue size per SSE subscriber.
_DEFAULT_MAXSIZE = 100


class RepeaterState:
    """In-memory repeater state: slot value, revision, SSE subscribers.

    All state is ephemeral and process-local.  On process restart everything
    resets to defaults.

    Args:
        tokens: Set of valid bearer tokens.  ``None`` means accept all
            (open mode, for local dev).  An empty set means refuse all.
        maxsize: Bounded queue size per SSE subscriber.  When a subscriber's
            queue is full, events are dropped for that subscriber.
    """

    def __init__(
        self,
        *,
        tokens: set[str] | list[str] | None = None,
        maxsize: int = _DEFAULT_MAXSIZE,
    ) -> None:
        self._slot_value: str = ""
        self._revision: int = 0
        self._subscribers: set[queue.Queue[str]] = set()
        self._lock = threading.Lock()
        self._maxsize = maxsize
        if tokens is None:
            self._tokens: set[str] | None = None
        else:
            self._tokens = set(tokens)

    # ── Properties ──────────────────────────────────────────────────

    @property
    def value(self) -> str:
        with self._lock:
            return self._slot_value

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    # ── Write ───────────────────────────────────────────────────────

    def write(self, value: str) -> int:
        """Store *value*, bump revision, push SSE event to subscribers.

        Returns the new revision number.
        """
        with self._lock:
            self._slot_value = value
            self._revision += 1
            rev = self._revision
            event = (
                f"event: write\n"
                f"data: {json.dumps({'revision': rev, 'value': value})}\n\n"
            )
            for q in self._subscribers:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass  # drop event for slow subscriber
            return rev

    # ── Snapshot ───────────────────────────────────────────────────

    def snapshot(self) -> tuple[str, int]:
        """Return ``(slot_value, revision)`` under lock."""
        with self._lock:
            return self._slot_value, self._revision

    # ── Subscribers ────────────────────────────────────────────────

    def add_subscriber(self) -> queue.Queue[str]:
        """Create a bounded queue, add to subscriber set, return it."""
        q: queue.Queue[str] = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.add(q)
        return q

    def remove_subscriber(self, q: queue.Queue[str]) -> None:
        """Remove *q* from the subscriber set."""
        with self._lock:
            self._subscribers.discard(q)

    # ── Auth ────────────────────────────────────────────────────────

    def validate_token(self, token: str) -> bool:
        """Constant-time token validation.

        ``tokens=None`` → accept all (open mode).
        ``tokens=set()`` → refuse all.
        """
        if self._tokens is None:
            return True
        if not self._tokens:
            return False
        for known in self._tokens:
            if hmac.compare_digest(token, known):
                return True
        return False