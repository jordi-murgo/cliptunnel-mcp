# tests/test_ws_repeater_server.py
from __future__ import annotations

import asyncio
import json
import unittest

from cliptunnel_mcp.ws_repeater.server import handler_factory
from cliptunnel_mcp.ws_repeater.state import WsRepeaterState


class _FakeWebSocket:
    """Minimal fake WebSocket for testing the handler coroutine."""

    def __init__(self) -> None:
        self._sent: list[str] = []
        self._recv_queue: asyncio.Queue = asyncio.Queue()
        self.closed = False

    async def send(self, msg: str) -> None:
        self._sent.append(msg)

    async def recv(self) -> str:
        return await self._recv_queue.get()

    def feed(self, msg: str) -> None:
        self._recv_queue.put_nowait(msg)

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.closed:
            raise StopAsyncIteration
        try:
            return await asyncio.wait_for(self._recv_queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            raise StopAsyncIteration


class TestHandlerAuth(unittest.TestCase):
    def test_valid_auth_sends_snapshot(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=["good"])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            ws.feed(json.dumps({"type": "auth", "token": "good"}))

            # Run handler briefly — it should send a snapshot
            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # First sent message should be snapshot
            self.assertGreater(len(ws._sent), 0)
            msg = json.loads(ws._sent[0])
            self.assertEqual(msg["type"], "snapshot")

        asyncio.run(run_test())

    def test_invalid_auth_sends_error_and_closes(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=["good"])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            ws.feed(json.dumps({"type": "auth", "token": "bad"}))

            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            self.assertGreater(len(ws._sent), 0)
            msg = json.loads(ws._sent[0])
            self.assertEqual(msg["type"], "error")
            self.assertEqual(msg["code"], "unauthorized")

        asyncio.run(run_test())

    def test_missing_auth_frame_times_out(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=["good"])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            # Don't feed anything — handler should time out and return
            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.2)
            # Task should have completed (timeout) or be cancelled
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        asyncio.run(run_test())


class TestHandlerWrite(unittest.TestCase):
    def test_write_returns_write_ack_and_pushes_event(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=["t"])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            ws.feed(json.dumps({"type": "auth", "token": "t"}))
            ws.feed(json.dumps({"type": "write", "value": "hello"}))

            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Should have sent snapshot then write_ack
            self.assertGreaterEqual(len(ws._sent), 2)
            msg0 = json.loads(ws._sent[0])
            self.assertEqual(msg0["type"], "snapshot")
            ack = json.loads(ws._sent[1])
            self.assertEqual(ack["type"], "write_ack")
            self.assertEqual(ack["revision"], 1)

        asyncio.run(run_test())


class TestHandlerPing(unittest.TestCase):
    def test_ping_returns_pong(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=["t"])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            ws.feed(json.dumps({"type": "auth", "token": "t"}))
            ws.feed(json.dumps({"type": "ping"}))

            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # Find the pong in sent messages (after snapshot)
            types = [json.loads(m)["type"] for m in ws._sent]
            self.assertIn("pong", types)

        asyncio.run(run_test())


class TestHandlerEmptyTokens(unittest.TestCase):
    def test_empty_tokens_refuse_all(self) -> None:
        async def run_test() -> None:
            state = WsRepeaterState(tokens=[])
            handler = handler_factory(state)
            ws = _FakeWebSocket()
            ws.feed(json.dumps({"type": "auth", "token": "anything"}))

            task = asyncio.create_task(handler(ws))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            self.assertGreater(len(ws._sent), 0)
            msg = json.loads(ws._sent[0])
            self.assertEqual(msg["code"], "unauthorized")

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()