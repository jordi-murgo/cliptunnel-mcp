"""In-process test double for the HTTPS repeater.

Implements the :class:`~cliptunnel_mcp.https_transport.HttpClient` protocol
so that :class:`~cliptunnel_mcp.https_transport.HttpsTransport` can be tested
without any network I/O.

The fake simulates three repeater endpoints:
  * ``POST /slot``     — stores a value, bumps revision, pushes SSE event
  * ``GET /slot``      — returns snapshot ``{"revision": N, "value": V}``
  * ``GET /slot/events`` — returns a :class:`FakeSseStream` that yields SSE lines

Convenience wrappers (:meth:`post_slot`, :meth:`get_slot`) make test code
readable while the protocol methods (:meth:`post`, :meth:`get`,
:meth:`open_stream`) are what :class:`HttpsTransport` calls.

Thread-safe via :class:`threading.Condition`, mirroring the
:class:`tests.clipboard_slot.ClipboardSlot` pattern.
"""
from __future__ import annotations

import hmac
import json
import queue
import threading
from dataclasses import dataclass


__all__ = ["FakeRepeater", "FakeSseStream", "HttpResponse"]


@dataclass
class HttpResponse:
    """Minimal HTTP response — ``status`` code and ``body`` string."""

    status: int
    body: str


class FakeSseStream:
    """Iterable of SSE lines backed by a :class:`queue.Queue`.

    ``FakeRepeater.post`` pushes fully-formatted SSE event strings (one
    line per queue entry).  ``__iter__`` pops from the queue with a short
    timeout so it can check the ``_closed`` flag between pops, allowing
    clean teardown.
    """

    def __init__(self, q: "queue.Queue[str]") -> None:
        self._queue = q
        self._closed = False

    def __iter__(self) -> "FakeSseStream":  # noqa: D401
        return self

    def __next__(self) -> str:
        while not self._closed:
            try:
                line = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            return line
        raise StopIteration

    def close(self) -> None:
        """Mark the stream as closed; the iterator will stop on the next poll."""
        self._closed = True


class FakeRepeater:
    """In-process repeater double — no sockets, no network.

    Parameters
    ----------
    tokens:
        When ``None`` (default), any token is accepted.  When a list of
        strings is given, only those exact tokens pass validation
        (compared with :func:`hmac.compare_digest`).
    """

    def __init__(self, *, tokens: list[str] | None = None) -> None:
        self._condition = threading.Condition()
        self._value: str = ""
        self._revision: int = 0
        self._writes: list[str] = []
        self._tokens = tokens
        # Each SSE subscriber gets a FakeSseStream and its queue.
        self._stream_objects: list[FakeSseStream] = []

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    def _validate_token(self, token: str) -> bool:
        """Return True if the token is accepted."""
        if self._tokens is None:
            return True
        return any(hmac.compare_digest(token, t) for t in self._tokens)

    def _extract_token(self, headers: dict[str, str]) -> str:
        """Extract bearer token from ``Authorization: Bearer <token>``."""
        auth = headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):]
        return ""

    def _check_auth(self, headers: dict[str, str]) -> bool:
        return self._validate_token(self._extract_token(headers))

    # ------------------------------------------------------------------
    # HttpClient protocol methods (called by HttpsTransport)
    # ------------------------------------------------------------------

    def post(
        self,
        url: str,
        headers: dict[str, str],
        body: str,
        timeout: float,
    ) -> HttpResponse:
        """Handle ``POST /slot`` — store body, bump revision, push SSE event."""
        if not self._check_auth(headers):
            return HttpResponse(401, json.dumps({"error": "unauthorized"}))

        with self._condition:
            self._value = body
            self._revision += 1
            self._writes.append(body)
            event_data = json.dumps({"revision": self._revision, "value": body})
            # Push SSE event lines to every active stream
            for s in self._stream_objects:
                try:
                    s._queue.put_nowait("event: write\n")
                    s._queue.put_nowait(f"data: {event_data}\n")
                    s._queue.put_nowait("\n")
                except queue.Full:
                    pass  # drop event for slow subscriber
            self._condition.notify_all()

        return HttpResponse(200, json.dumps({"revision": self._revision}))

    def get(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpResponse:
        """Handle ``GET /slot`` — return current snapshot."""
        if not self._check_auth(headers):
            return HttpResponse(401, json.dumps({"error": "unauthorized"}))

        with self._condition:
            snapshot = json.dumps(
                {"revision": self._revision, "value": self._value}
            )
        return HttpResponse(200, snapshot)

    def open_stream(
        self,
        url: str,
        headers: dict[str, str],
    ) -> FakeSseStream:
        """Handle ``GET /slot/events`` — return a :class:`FakeSseStream`."""
        if not self._check_auth(headers):
            raise PermissionError("unauthorized")

        q: "queue.Queue[str]" = queue.Queue(maxsize=100)
        stream = FakeSseStream(q)
        with self._condition:
            self._stream_objects.append(stream)
        return stream

    # ------------------------------------------------------------------
    # Convenience wrappers (for test readability)
    # ------------------------------------------------------------------

    def post_slot(self, body: str, token: str) -> tuple[int, str]:
        """Convenience: ``POST /slot`` with a bare token."""
        resp = self.post(
            "http://fake/slot",
            {"Authorization": f"Bearer {token}"},
            body,
            15.0,
        )
        return resp.status, resp.body

    def get_slot(self, token: str) -> tuple[int, str]:
        """Convenience: ``GET /slot`` with a bare token."""
        resp = self.get(
            "http://fake/slot",
            {"Authorization": f"Bearer {token}"},
            15.0,
        )
        return resp.status, resp.body

    # ------------------------------------------------------------------
    # SSE test helpers
    # ------------------------------------------------------------------

    def push_keepalive(self) -> None:
        """Push a ``: keepalive\\n\\n`` comment to all active streams."""
        with self._condition:
            for s in self._stream_objects:
                try:
                    s._queue.put_nowait(": keepalive\n")
                    s._queue.put_nowait("\n")
                except queue.Full:
                    pass

    def drop_all_streams(self) -> None:
        """Close and remove all active SSE streams (simulate disconnect)."""
        with self._condition:
            for s in self._stream_objects:
                s.close()
            self._stream_objects.clear()
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Inspection properties
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    @property
    def value(self) -> str:
        with self._condition:
            return self._value

    @property
    def writes(self) -> list[str]:
        with self._condition:
            return list(self._writes)