# tests/test_ws_repeater_state.py
from __future__ import annotations

import asyncio
import unittest

from cliptunnel_mcp.ws_repeater.state import WsRepeaterState


class TestWsRepeaterStateWrite(unittest.TestCase):
    def test_write_stores_value_and_bumps_revision(self) -> None:
        s = WsRepeaterState()
        rev = asyncio.run(s.write("hello"))
        self.assertEqual(rev, 1)
        val, r = asyncio.run(s.snapshot())
        self.assertEqual(val, "hello")
        self.assertEqual(r, 1)

    def test_multiple_writes_increment(self) -> None:
        s = WsRepeaterState()
        asyncio.run(s.write("a"))
        asyncio.run(s.write("b"))
        asyncio.run(s.write("c"))
        val, rev = asyncio.run(s.snapshot())
        self.assertEqual(val, "c")
        self.assertEqual(rev, 3)

    def test_snapshot_returns_current_state(self) -> None:
        s = WsRepeaterState()
        asyncio.run(s.write("data"))
        val, rev = asyncio.run(s.snapshot())
        self.assertEqual(val, "data")
        self.assertEqual(rev, 1)


class TestWsRepeaterStateSubscribers(unittest.TestCase):
    def test_add_remove_subscriber(self) -> None:
        s = WsRepeaterState()
        q = asyncio.run(s.add_subscriber())
        self.assertIsNotNone(q)
        asyncio.run(s.remove_subscriber(q))

    def test_write_pushes_event_to_other_subscribers(self) -> None:
        async def run_test() -> None:
            s = WsRepeaterState()
            q = await s.add_subscriber()
            # Write from a "different" connection — simulate by writing directly.
            await s.write("pushed")
            # The event should be in the subscriber's queue.
            event = await asyncio.wait_for(q.get(), timeout=1.0)
            self.assertEqual(event["type"], "event")
            self.assertEqual(event["value"], "pushed")
            self.assertEqual(event["revision"], 1)

        asyncio.run(run_test())

    def test_full_queue_drops_event(self) -> None:
        async def run_test() -> None:
            s = WsRepeaterState(maxsize=2)
            q = await s.add_subscriber()
            await s.write("a")
            await s.write("b")
            # Queue is now full (maxsize=2); next write should drop, not block.
            await s.write("c")
            self.assertLessEqual(q.qsize(), 2)

        asyncio.run(run_test())

    def test_concurrent_writes_serialize(self) -> None:
        async def run_test() -> None:
            s = WsRepeaterState()

            async def writer(n: int) -> None:
                for i in range(50):
                    await s.write(f"w{n}-{i}")

            await asyncio.gather(*(writer(i) for i in range(4)))
            _, rev = await s.snapshot()
            self.assertEqual(rev, 200)  # 4 * 50

        asyncio.run(run_test())


class TestWsRepeaterStateTokens(unittest.TestCase):
    def test_validate_token_accept_all_when_none(self) -> None:
        s = WsRepeaterState(tokens=None)
        self.assertTrue(s.validate_token("anything"))

    def test_validate_token_accepts_listed(self) -> None:
        s = WsRepeaterState(tokens=["good"])
        self.assertTrue(s.validate_token("good"))

    def test_validate_token_rejects_unlisted(self) -> None:
        s = WsRepeaterState(tokens=["good"])
        self.assertFalse(s.validate_token("bad"))

    def test_validate_token_empty_set_refuses_all(self) -> None:
        s = WsRepeaterState(tokens=[])
        self.assertFalse(s.validate_token("anything"))


if __name__ == "__main__":
    unittest.main()