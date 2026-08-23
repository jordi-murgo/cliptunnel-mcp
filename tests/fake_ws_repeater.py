# tests/fake_ws_repeater.py
"""In-process test double for the WebSocket repeater.

Implements a synchronous test interface (``connect``, ``send``,
``recv_nowait``, ``close`` on connections) and an async WsClient interface
(``connect``, ``send``, ``recv``, ``close`` on the repeater) backed by
``threading.Condition`` — no ``websockets`` library, no network sockets.

Used by ``WebSocketTransport`` tests via the ``ws_client`` injection
parameter, and by ``test_fake_ws_repeater.py`` tests directly.

Frame protocol (JSON, one per message):
  Client → repeater:
    ``{"type": "auth", "token": "..."}``  — authenticate
    ``{"type": "write", "value": "..."}`` — store value, push event
    ``{"type": "ping"}``                   — keepalive
  Repeater → client:
    ``{"type": "snapshot", "value": "...", "revision": N}`` — initial state
    ``{"type": "write_ack", "revision": N}``                — write confirmed
    ``{"type": "event", "value": "...", "revision": N}``    — pushed update
    ``{"type": "pong"}``                                     — keepalive reply
    ``{"type": "error", "code": "unauthorized"}``           — auth failure
"""
from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from typing import Protocol, runtime_checkable

from cliptunnel_mcp.https_transport import TransportAuthError

__all__ = ["FakeWsRepeater", "FakeWsConnection", "WsClient"]


@runtime_checkable
class WsClient(Protocol):
    """Minimal async WebSocket client interface for adapter injection."""

    async def connect(self, url: str) -> None: ...
    async def send(self, msg: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class FakeWsConnection:
    """A single client connection to the FakeWsRepeater.

    The ``_inbox`` list holds frames queued for this connection (snapshot,
    events, write_ack, pong). ``send`` synchronously processes a frame
    and returns the immediate response (write_ack, pong) or the snapshot
    (for auth, which is also queued into ``_inbox``).
    """

    def __init__(self, repeater: "FakeWsRepeater") -> None:
        self._repeater: FakeWsRepeater = repeater
        self._inbox: list[str] = []
        self._closed: bool = False
        self._authenticated: bool = False

    # -- awaitable: allows ``await ws_client.connect(url)`` (WsClient) --
    def __await__(self):
        return iter([])  # immediately returns None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    @property
    def closed(self) -> bool:
        return self._closed

    def _process(self, msg: str) -> str:
        """Process a client→repeater frame and return the response string.

        For ``auth``: validates token, sets ``_authenticated``, returns
        the snapshot JSON. Does NOT queue into inbox (caller decides).
        For ``write``: calls ``repeater._handle_write``, which pushes
        events to other connections and returns ``write_ack``.
        For ``ping``: returns ``pong``.
        """
        data = json.loads(msg)
        msg_type = data.get("type")

        if msg_type == "auth":
            token = data.get("token", "")
            if not self._repeater._validate_token(token):
                raise TransportAuthError("invalid token")
            self._authenticated = True
            with self._repeater._condition:
                snapshot = json.dumps({
                    "type": "snapshot",
                    "value": self._repeater._slot_value,
                    "revision": self._repeater._revision,
                })
            return snapshot

        if msg_type == "write":
            value = data.get("value", "")
            return self._repeater._handle_write(self, value)

        if msg_type == "ping":
            return json.dumps({"type": "pong"})

        return json.dumps({"type": "error", "code": "bad_request"})

    def send(self, msg: str) -> str:
        """Process a frame and return the response.

        For ``auth``: also queues the snapshot into ``_inbox`` so
        ``recv_nowait`` can pick it up.
        """
        response = self._process(msg)
        data = json.loads(msg)
        if data.get("type") == "auth":
            self._inbox.append(response)
        return response

    def recv_nowait(self) -> str:
        """Pop and return the next queued frame, or raise ``IndexError``."""
        if self._inbox:
            return self._inbox.pop(0)
        raise IndexError("inbox empty")

    def recv(self, timeout: float = 5.0) -> str:
        """Blocking pop with timeout (uses the repeater's Condition)."""
        with self._repeater._condition:
            end = time.monotonic() + timeout
            while not self._inbox and time.monotonic() < end:
                self._repeater._condition.wait(timeout=end - time.monotonic())
            if self._inbox:
                return self._inbox.pop(0)
        raise TimeoutError("recv timed out")

    def close(self) -> None:
        """Mark this connection as closed and remove it from the repeater."""
        self._closed = True
        with self._repeater._condition:
            if self in self._repeater._connections:
                self._repeater._connections.remove(self)
            self._repeater._condition.notify_all()


class FakeWsRepeater:
    """In-process WebSocket repeater double — no sockets, no network.

    Serves two roles:
    1. **Sync test helper**: ``r.connect("token")`` returns a
       ``FakeWsConnection`` (authenticated, with snapshot queued).
    2. **Async WsClient**: ``await r.connect(url)``, ``await r.send(msg)``,
       ``await r.recv()``, ``await r.close()`` — for ``WebSocketTransport``
       injection via the ``ws_client`` parameter.

    Parameters
    ----------
    tokens:
        When ``None`` (default), any token is accepted.  When a list of
        strings is given, only those exact tokens pass validation.
    """

    def __init__(self, *, tokens: list[str] | None = None) -> None:
        self._condition: threading.Condition = threading.Condition()
        self._slot_value: str = ""
        self._revision: int = 0
        self._writes: list[str] = []
        self._connections: list[FakeWsConnection] = []
        self._tokens: list[str] | None = tokens
        self._current_conn: FakeWsConnection | None = None

    # ------------------------------------------------------------------
    # Token validation
    # ------------------------------------------------------------------

    def _validate_token(self, token: str) -> bool:
        if self._tokens is None:
            return True
        return any(hmac.compare_digest(token, t) for t in self._tokens)

    # ------------------------------------------------------------------
    # Sync connect (test helper) / async connect (WsClient protocol)
    # ------------------------------------------------------------------

    def connect(self, token_or_url: str) -> FakeWsConnection:
        """Create a connection.

        When *token_or_url* looks like a token (does not start with
        ``ws://`` or ``wss://``), the connection is authenticated
        immediately and the snapshot is queued into its inbox. This is
        the synchronous test-helper path.

        When *token_or_url* starts with ``ws://`` or ``wss://``, the
        connection is created without authentication — the caller (the
        transport) sends an ``auth`` frame separately via ``send``.
        The returned ``FakeWsConnection`` is awaitable (``__await__``
        returns ``iter([])``) so ``await ws_client.connect(url)`` works.
        """
        conn = FakeWsConnection(self)
        with self._condition:
            self._connections.append(conn)
            self._current_conn = conn

        if not (token_or_url.startswith("ws://") or token_or_url.startswith("wss://")):
            # Token — authenticate immediately (test helper path)
            auth_msg = json.dumps({"type": "auth", "token": token_or_url})
            response = conn._process(auth_msg)  # may raise TransportAuthError
            conn._inbox.append(response)

        return conn

    # ------------------------------------------------------------------
    # Async WsClient protocol methods (used by WebSocketTransport)
    # ------------------------------------------------------------------

    async def send(self, msg: str) -> None:
        """Process a frame on the current connection and queue the response."""
        if self._current_conn is None:
            raise ConnectionError("not connected")
        response = self._current_conn._process(msg)
        if response:
            self._current_conn._inbox.append(response)

    async def recv(self) -> str:
        """Pop the next frame from the current connection's inbox.

        Polls with small async sleeps so the event loop can run other
        coroutines (e.g., ``_send_write``) while waiting. Raises
        ``ConnectionError`` if the connection is closed.
        """
        while True:
            if self._current_conn is None or self._current_conn.closed:
                raise ConnectionError("connection closed")
            try:
                return self._current_conn.recv_nowait()
            except IndexError:
                await asyncio.sleep(0.01)

    async def close(self) -> None:
        """Close the current connection."""
        if self._current_conn is not None:
            self._current_conn.close()
            self._current_conn = None

    # ------------------------------------------------------------------
    # Write handling
    # ------------------------------------------------------------------

    def _handle_write(self, conn: FakeWsConnection, value: str) -> str:
        """Store value, bump revision, push event to other connections.

        Returns ``write_ack`` JSON string.
        """
        with self._condition:
            self._slot_value = value
            self._revision += 1
            rev = self._revision
            self._writes.append(value)
            event = json.dumps({
                "type": "event",
                "value": value,
                "revision": rev,
            })
            for other in self._connections:
                if other is conn or not other._authenticated:
                    continue
                other._inbox.append(event)
            self._condition.notify_all()

        return json.dumps({"type": "write_ack", "revision": rev})

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
            return self._slot_value

    @property
    def writes(self) -> list[str]:
        with self._condition:
            return list(self._writes)

    @property
    def connections(self) -> list[FakeWsConnection]:
        with self._condition:
            return list(self._connections)