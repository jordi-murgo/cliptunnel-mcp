"""WebSocket transport — Transport + RevisionMonitor over WS frames.

Implements :class:`~cliptunnel_mcp.transport.Transport` and
:class:`~cliptunnel_mcp.transport.RevisionMonitor` against a WebSocket
repeater using a JSON frame protocol.

The Transport/RevisionMonitor protocols are synchronous; ``websockets``
is asyncio. The bridge is a daemon thread running a private
``asyncio.new_event_loop()`` with ``run_coroutine_threadsafe`` dispatching
coroutines from the sync side. A ``threading.Condition`` synchronizes
the shared ``_value``/``_revision`` state.

Frame protocol (JSON, one per WS message):
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

Auth failures raise :class:`~cliptunnel_mcp.https_transport.TransportAuthError`;
other failures raise :class:`~cliptunnel_mcp.https_transport.TransportError`.
``websockets`` is an optional extra (lazy import inside ``__init__`` probe
and ``_connect``). Importing this module never pulls in ``websockets``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Protocol, runtime_checkable

from cliptunnel_mcp.https_transport import TransportAuthError, TransportError

__all__ = ["WebSocketTransport", "WsClient"]

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# WsClient protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class WsClient(Protocol):
    """Minimal async WebSocket client interface for adapter injection."""

    async def connect(self, url: str) -> None: ...
    async def send(self, msg: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Production WsClient (wraps websockets.connect)
# ---------------------------------------------------------------------------


class _WebsocketsClient:
    """Production WsClient wrapping ``websockets.connect()``.

    Created lazily inside ``WebSocketTransport.__init__`` only when
    ``ws_client`` is not provided.
    """

    def __init__(self) -> None:
        self._ws = None

    async def connect(self, url: str) -> None:
        import websockets  # lazy import

        # Close any existing connection before opening a new one
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        self._ws = await websockets.connect(url, ping_interval=None, ping_timeout=None, close_timeout=None)

    async def send(self, msg: str) -> None:
        if self._ws is None:
            raise TransportError("not connected")
        await self._ws.send(msg)

    async def recv(self) -> str:
        if self._ws is None:
            raise TransportError("not connected")
        return await self._ws.recv()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None


# ---------------------------------------------------------------------------
# WebSocketTransport
# ---------------------------------------------------------------------------


class WebSocketTransport:
    """Transport + RevisionMonitor backed by a WebSocket repeater.

    Parameters
    ----------
    ws_url:
        WebSocket URL of the repeater (e.g. ``ws://relay:9000``).
    bearer_token:
        Token sent in the ``auth`` frame after connecting.
    poll_timeout:
        Seconds before ``wait_for_change`` returns on no change.
    reconnect_delay:
        Initial delay before reconnecting after a drop. Doubled on
        consecutive failures, capped at ``reconnect_max_delay``.
    reconnect_max_delay:
        Maximum reconnect delay.
    request_timeout:
        Timeout for waiting on ``write_ack`` and initial snapshot.
    ws_client:
        Optional :class:`WsClient` for dependency injection.
        When ``None``, uses the built-in :class:`_WebsocketsClient`
        (which lazy-imports ``websockets``).
    """

    def __init__(
        self,
        *,
        ws_url: str,
        bearer_token: str,
        poll_timeout: float = 30.0,
        reconnect_delay: float = 1.0,
        reconnect_max_delay: float = 30.0,
        request_timeout: float = 15.0,
        ws_client: WsClient | None = None,
    ) -> None:
        self._ws_url = ws_url
        self._bearer_token = bearer_token
        self._poll_timeout = poll_timeout
        self._reconnect_delay = reconnect_delay
        self._reconnect_max_delay = reconnect_max_delay
        self._request_timeout = request_timeout

        self._condition = threading.Condition()
        self._value: str = ""
        self._revision: int = 0
        self._running: bool = True

        # Write acknowledgment signaling (asyncio.Event, set from receive loop)
        self._write_ack_event: asyncio.Event | None = None
        self._write_ack_revision: int = 0

        # Auth error stored from the connect/receive coroutine so that
        # write() can re-raise it instead of timing out.
        self._auth_error: TransportAuthError | None = None

        # Probe-import websockets if using the production client
        if ws_client is None:
            try:
                import websockets  # noqa: F401
            except ImportError as exc:
                raise TransportError(
                    "WebSocketTransport requires the 'websockets' package. "
                    "Install it with: pip install cliptunnel-mcp[websocket]"
                ) from exc
            ws_client = _WebsocketsClient()

        self._ws_client: WsClient = ws_client
        self._connected: bool = False

        # Private asyncio event loop running on a daemon thread
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever,
            daemon=True,
            name="WebSocketTransport-Loop",
        )
        self._loop_thread.start()

        # Dispatch the main connect-and-receive coroutine
        self._main_future = asyncio.run_coroutine_threadsafe(
            self._connect_and_receive(), self._loop
        )

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    def read(self) -> str:
        """Return the locally cached slot value. Never blocks."""
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        """Send a ``write`` frame and wait for ``write_ack``.

        Raises :class:`TransportAuthError` on auth failure and
        :class:`TransportError` on timeout or network error.
        """
        if not self._running:
            raise TransportError("transport is closed")

        future = asyncio.run_coroutine_threadsafe(
            self._send_write(value), self._loop
        )
        try:
            future.result(timeout=self._request_timeout)
        except TransportAuthError:
            raise
        except TransportError:
            raise
        except Exception as exc:
            raise TransportError(f"write failed: {exc}") from exc

    # ------------------------------------------------------------------
    # RevisionMonitor protocol
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
        """Block until ``revision > after`` or timeout, then return revision."""
        with self._condition:
            self._condition.wait_for(
                lambda: self._revision > after, timeout=timeout
            )
            return self._revision

    # ------------------------------------------------------------------
    # Async coroutines (run on the private event loop)
    # ------------------------------------------------------------------

    async def _connect_and_receive(self) -> None:
        """Main coroutine: connect, receive, reconnect on drop."""
        delay = self._reconnect_delay
        while self._running:
            try:
                await self._connect()
                await self._receive_loop()
                _log.warning("WS _receive_loop exited — connection dropped")
                # _receive_loop returned — connection dropped, close it
                try:
                    await self._ws_client.close()
                except Exception:
                    pass
            except TransportAuthError as exc:
                _log.error("WS auth failure — stopping: %s", exc)
                self._auth_error = exc
                self._connected = False
                return
            except Exception as exc:
                _log.warning("WS connection error: %s", exc)
                self._connected = False
                # Close the old connection before reconnecting
                try:
                    await self._ws_client.close()
                except Exception:
                    pass

            # Exponential backoff before reconnecting
            delay = min(delay * 2, self._reconnect_max_delay)
            _log.debug("WS reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)

    async def _connect(self) -> None:
        """Open the WS connection, send auth frame, wait for snapshot."""
        await self._ws_client.connect(self._ws_url)
        self._connected = True

        # Send auth frame
        auth_msg = json.dumps({"type": "auth", "token": self._bearer_token})
        await self._ws_client.send(auth_msg)

        # Wait for snapshot response
        try:
            raw = await asyncio.wait_for(
                self._ws_client.recv(), timeout=self._request_timeout
            )
        except asyncio.TimeoutError:
            raise TransportError("timeout waiting for snapshot after auth")

        msg = json.loads(raw)
        msg_type = msg.get("type")

        if msg_type == "error" and msg.get("code") == "unauthorized":
            raise TransportAuthError("auth rejected by repeater")

        if msg_type != "snapshot":
            raise TransportError(f"expected snapshot, got: {msg_type}")

        value = msg.get("value", "")
        revision = msg.get("revision", 0)

        with self._condition:
            self._value = value
            self._revision = max(self._revision, revision)
            self._condition.notify_all()

    async def _receive_loop(self) -> None:
        """Read frames from the WS connection until it drops.

        Sends a keepalive ping every 20s of inactivity to prevent the
        server from closing the connection.
        """
        while self._running:
            try:
                raw = await asyncio.wait_for(
                    self._ws_client.recv(), timeout=20.0
                )
            except asyncio.TimeoutError:
                # No frame in 20s — send keepalive ping
                try:
                    await self._ws_client.send(json.dumps({"type": "ping"}))
                except Exception as exc:
                    _log.debug("WS keepalive ping failed: %s", exc)
                    raise
                continue

            if not self._running:
                break

            msg = json.loads(raw)
            msg_type = msg.get("type")

            if msg_type == "event":
                value = msg.get("value", "")
                revision = msg.get("revision", 0)
                with self._condition:
                    self._value = value
                    self._revision = max(self._revision, revision)
                    self._condition.notify_all()

            elif msg_type == "snapshot":
                value = msg.get("value", "")
                revision = msg.get("revision", 0)
                with self._condition:
                    self._value = value
                    self._revision = max(self._revision, revision)
                    self._condition.notify_all()

            elif msg_type == "write_ack":
                revision = msg.get("revision", 0)
                if self._write_ack_event is not None:
                    self._write_ack_revision = revision
                    self._write_ack_event.set()

            elif msg_type == "ping":
                # Server-initiated ping — respond with pong
                try:
                    await self._ws_client.send(json.dumps({"type": "pong"}))
                except Exception:
                    pass

            elif msg_type == "pong":
                pass  # our keepalive ping was answered

            elif msg_type == "error":
                code = msg.get("code", "")
                _log.warning("WS error frame: %s", msg)
                if code == "unauthorized":
                    raise TransportAuthError("unauthorized by repeater")

            else:
                _log.debug("WS unknown frame type: %s", msg_type)

    async def _send_write(self, value: str) -> None:
        """Send a write frame and wait for the write_ack event."""
        if self._auth_error is not None:
            raise self._auth_error
        if not self._connected:
            raise TransportError("not connected")

        self._write_ack_event = asyncio.Event()
        self._write_ack_revision = 0

        write_msg = json.dumps({"type": "write", "value": value})
        await self._ws_client.send(write_msg)

        try:
            await asyncio.wait_for(
                self._write_ack_event.wait(), timeout=self._request_timeout
            )
        except asyncio.TimeoutError:
            raise TransportError("timeout waiting for write_ack")

        ack_rev = self._write_ack_revision
        with self._condition:
            self._value = value
            self._revision = max(self._revision, ack_rev)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        return "websocket"

    @property
    def endpoint(self) -> str | None:
        """Sanitized transport endpoint for sysinfo (no bearer token)."""
        return self._ws_url

    def close(self) -> None:
        """Stop the background loop and close the WS connection. Idempotent."""
        if not self._running:
            return
        self._running = False

        with self._condition:
            self._condition.notify_all()

        # Close the WS client
        if self._connected:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    self._ws_client.close(), self._loop
                )
                fut.result(timeout=2.0)
            except Exception:
                pass

        # Stop the event loop
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._loop_thread.is_alive():
            self._loop_thread.join(timeout=2.0)

        self._connected = False