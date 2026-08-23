"""In-process test double for the Firebase Realtime Database REST API.

Implements the :class:`~cliptunnel_mcp.https_transport.HttpClient` protocol
(plus the ``put`` verb :class:`~cliptunnel_mcp.firebase_transport.FirebaseTransport`
uses for node replacement) so the transport can be tested without any
network I/O.

The fake simulates one RTDB node (default path ``/cliptunnel``) shaped
``{"v": <str>, "r": <server timestamp ms>}``:

  * ``PUT /<node>.json`` — stores the node (resolving the
    ``{".sv": "timestamp"}`` placeholder to a monotonic ms counter),
    pushes an SSE ``put`` event to every open stream, and returns the
    stored node as JSON.
  * ``GET /<node>.json`` — returns the current node snapshot, or the
    literal ``null`` body when the database is empty.
  * ``GET /<node>.json`` with ``Accept: text/event-stream`` — returns a
    :class:`~tests.fake_repeater.FakeSseStream` that first yields the
    initial ``put`` event carrying the current node, then queued events.

Auth: when a ``token`` is configured, every call must carry
``Authorization: Bearer <token>``; mismatches get HTTP 401.

Thread-safe via :class:`threading.Condition`, mirroring
:class:`tests.fake_repeater.FakeRepeater`.
"""
from __future__ import annotations

import hmac
import json
import queue
import threading

from cliptunnel_mcp.https_transport import HttpResponse
from tests.fake_repeater import FakeSseStream

__all__ = ["FakeFirebase"]


class FakeFirebase:
    """In-process Firebase RTDB double — no sockets, no network.

    Parameters
    ----------
    token:
        When ``None`` (default), any token is accepted.  When a string
        is given, only that exact token passes validation (compared
        with :func:`hmac.compare_digest`).
    """

    def __init__(self, *, token: str | None = None) -> None:
        self._condition = threading.Condition()
        self._node: dict[str, object] | None = None
        # Monotonic millisecond "server clock": starts at a plausible
        # epoch-ms base so revisions look like real Firebase timestamps
        # and strictly increase across writes.
        self._clock_ms = 1_700_000_000_000
        self._token = token
        self._streams: list[FakeSseStream] = []
        # (url, parsed request body) of every accepted PUT — for wire
        # format assertions in tests.
        self._puts: list[tuple[str, dict]] = []

    # ------------------------------------------------------------------
    # Token validation (same shape as FakeRepeater)
    # ------------------------------------------------------------------

    def _check_auth(self, headers: dict[str, str]) -> bool:
        """Return True if the Authorization header carries the token."""
        if self._token is None:
            return True
        auth = headers.get("Authorization", "")
        token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        return hmac.compare_digest(token, self._token)

    def _push_event(self, event_data: str) -> None:
        """Queue an SSE ``put`` event on every open subscriber stream."""
        for s in self._streams:
            try:
                s._queue.put_nowait("event: put\n")
                s._queue.put_nowait(f"data: {event_data}\n")
                s._queue.put_nowait("\n")
            except queue.Full:
                pass  # drop event for slow subscriber

    # ------------------------------------------------------------------
    # HttpClient protocol methods (called by FirebaseTransport)
    # ------------------------------------------------------------------

    def put(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        timeout: float,
    ) -> HttpResponse:
        """Handle ``PUT /<node>.json`` — replace node, push SSE event."""
        if not self._check_auth(headers):
            return HttpResponse(401, json.dumps({"error": "unauthorized"}))

        payload = json.loads(body)  # {"v": ..., "r": {".sv": "timestamp"}}
        with self._condition:
            self._clock_ms += 1  # resolve the server-timestamp placeholder
            node: dict[str, object] = {"v": payload.get("v", ""), "r": self._clock_ms}
            self._node = node
            self._puts.append((url, payload))
            self._push_event(json.dumps({"path": "/", "data": node}))
            self._condition.notify_all()

        return HttpResponse(200, json.dumps(node))

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        timeout: float,
    ) -> HttpResponse:
        """Protocol alias — FirebaseTransport issues PUTs, never POSTs."""
        return self.put(url, headers, body, timeout)

    def get(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse:
        """Handle ``GET /<node>.json`` — return the current snapshot."""
        if not self._check_auth(headers):
            return HttpResponse(401, json.dumps({"error": "unauthorized"}))

        with self._condition:
            body = json.dumps(self._node) if self._node is not None else "null"
        return HttpResponse(200, body)

    def open_stream(
        self,
        url: str,
        headers: dict[str, str],
    ) -> FakeSseStream:
        """Handle the SSE request — return a stream seeded with the node."""
        if not self._check_auth(headers):
            raise PermissionError("unauthorized")

        q: "queue.Queue[str]" = queue.Queue(maxsize=100)
        stream = FakeSseStream(q)
        with self._condition:
            self._streams.append(stream)
            if self._node is not None:
                # Firebase's first SSE event is the current node state.
                self._push_event(json.dumps({"path": "/", "data": self._node}))
        return stream

    # ------------------------------------------------------------------
    # Test helpers
    # ------------------------------------------------------------------

    def drop_all_streams(self) -> None:
        """Close and remove all SSE streams (simulate disconnect)."""
        with self._condition:
            for s in self._streams:
                s.close()
            self._streams.clear()
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Inspection properties
    # ------------------------------------------------------------------

    @property
    def has_streams(self) -> bool:
        with self._condition:
            return bool(self._streams)

    @property
    def node(self) -> dict[str, object] | None:
        with self._condition:
            return dict(self._node) if self._node is not None else None

    @property
    def value(self) -> str:
        with self._condition:
            return str(self._node["v"]) if self._node is not None else ""

    @property
    def revision(self) -> int:
        with self._condition:
            return int(self._node["r"]) if self._node is not None else 0

    @property
    def puts(self) -> list[tuple[str, dict]]:
        with self._condition:
            return list(self._puts)
