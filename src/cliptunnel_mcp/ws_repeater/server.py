"""WebSocket repeater server — async handler for WS connections.

Provides ``handler_factory(state)`` which returns an async handler
coroutine compatible with ``websockets.serve()``. The handler authenticates
the client, sends an initial snapshot, then concurrently reads client
frames (write, ping) and pushes events to the client via a subscriber
queue.

``websockets`` is lazy-imported inside ``__main__`` only — the handler
itself uses only stdlib (``asyncio``, ``json``) so it can be tested with
a fake WebSocket that implements ``send``/``recv``/``close``/``__aiter__``.
"""
from __future__ import annotations

import asyncio
import json
import logging

from cliptunnel_mcp.ws_repeater.state import WsRepeaterState

__all__ = ["handler_factory"]

_log = logging.getLogger(__name__)

_AUTH_TIMEOUT = 10.0  # seconds to receive the auth frame


def handler_factory(state: WsRepeaterState):
    """Return an ``async def handler(websocket)`` coroutine.

    The handler closes over *state* and manages one client connection.
    """

    async def handler(websocket) -> None:
        # 1. Read the auth frame with a timeout.
        try:
            raw = await asyncio.wait_for(
                websocket.recv(), timeout=_AUTH_TIMEOUT
            )
        except (asyncio.TimeoutError, Exception):
            return  # client didn't auth in time or connection dropped

        # 2. Parse and validate the auth frame.
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            await _send(websocket, {"type": "error", "code": "bad_request",
                                    "message": "invalid JSON"})
            await _close(websocket)
            return

        if msg.get("type") != "auth":
            await _send(websocket, {"type": "error", "code": "bad_request",
                                    "message": "expected auth frame"})
            await _close(websocket)
            return

        token = msg.get("token", "")
        result = state.validate_token(token)
        if not result:
            _log.warning("auth failed — invalid token")
            await _send(websocket, {"type": "error", "code": "unauthorized",
                                    "message": "invalid token"})
            await _close(websocket)
            return
        name = result if isinstance(result, str) else "unnamed"
        _log.info("client connected (name=%s)", name)
        # 3. Send initial snapshot.
        value, revision = await state.snapshot()
        await _send(websocket, {
            "type": "snapshot",
            "value": value,
            "revision": revision,
        })

        # 4. Subscribe to events.
        q = await state.add_subscriber()
        try:
            # 5. Run read and push loops concurrently.
            read_task = asyncio.create_task(_read_loop(websocket, state))
            push_task = asyncio.create_task(_push_loop(websocket, q))
            done, pending = await asyncio.wait(
                {read_task, push_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        finally:
            await state.remove_subscriber(q)
            _log.info("client disconnected")

    return handler


async def _read_loop(websocket, state: WsRepeaterState) -> None:
    """Read client frames: write, ping. Runs until connection drops."""
    async for raw in websocket:
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        msg_type = msg.get("type")
        if msg_type == "write":
            value = msg.get("value", "")
            rev = await state.write(value)
            await _send(websocket, {"type": "write_ack", "revision": rev})
        elif msg_type == "ping":
            await _send(websocket, {"type": "pong"})
        # Ignore unknown types.


async def _push_loop(websocket, q: asyncio.Queue) -> None:
    """Push subscriber events to the client. Runs until connection drops."""
    while True:
        event = await q.get()
        try:
            await _send(websocket, event)
        except Exception:
            return  # connection closed


async def _send(websocket, msg: dict) -> None:
    """Send a JSON frame, catching connection errors."""
    await websocket.send(json.dumps(msg))


async def _close(websocket) -> None:
    """Close the websocket, catching errors."""
    try:
        await websocket.close()
    except Exception:
        pass