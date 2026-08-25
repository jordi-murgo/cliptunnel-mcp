"""Tests for the repeater service — T5.

Converted to unittest.TestCase so the CI runner (unittest discover) can
discover and run these tests without pytest installed.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from cliptunnel_mcp.repeater.server import make_handler, RepeaterServer
from cliptunnel_mcp.repeater.state import RepeaterState


# ---------------------------------------------------------------------------
# RepeaterState unit tests
# ---------------------------------------------------------------------------


class TestRepeaterState(unittest.TestCase):
    def test_write_stores_value_and_bumps_revision(self):
        s = RepeaterState()
        rev = s.write("hello")
        self.assertEqual(rev, 1)
        self.assertEqual(s.value, "hello")
        self.assertEqual(s.revision, 1)

    def test_snapshot_returns_value_and_revision(self):
        s = RepeaterState()
        s.write("data")
        val, rev = s.snapshot()
        self.assertEqual(val, "data")
        self.assertEqual(rev, 1)

    def test_multiple_writes_increment(self):
        s = RepeaterState()
        s.write("a")
        s.write("b")
        s.write("c")
        self.assertEqual(s.revision, 3)

    def test_add_remove_subscriber(self):
        s = RepeaterState()
        q = s.add_subscriber()
        self.assertIsNotNone(q)
        s.remove_subscriber(q)

    def test_write_pushes_to_subscribers(self):
        s = RepeaterState()
        q = s.add_subscriber()
        s.write("pushed")
        event = q.get(timeout=1.0)
        self.assertIn("write", event)
        self.assertIn("pushed", event)

    def test_full_queue_drops_event(self):
        s = RepeaterState(maxsize=2)
        q = s.add_subscriber()
        s.write("a")
        s.write("b")
        s.write("c")  # should not block
        self.assertLessEqual(q.qsize(), 2)

    def test_concurrent_writes_serialize(self):
        s = RepeaterState()

        def writer(n: int) -> None:
            for i in range(50):
                s.write(f"w{n}-{i}")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(s.revision, 200)  # 4 * 50


# ---------------------------------------------------------------------------
# HTTP handler tests (in-process ThreadingHTTPServer)
# ---------------------------------------------------------------------------


class TestRepeaterHTTP(unittest.TestCase):
    """Tests using a real in-process ThreadingHTTPServer."""

    def setUp(self) -> None:
        self._state = RepeaterState(tokens=["test-token"])
        handler = make_handler(self._state)
        self._srv = RepeaterServer(("127.0.0.1", 0), handler)
        self._port = self._srv.server_address[1]
        self._thread = threading.Thread(
            target=self._srv.serve_forever, daemon=True
        )
        self._thread.start()

    def tearDown(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    @property
    def _url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def test_post_slot_valid_token(self):
        req = urllib.request.Request(
            f"{self._url}/slot",
            data=b"hello",
            headers={"Authorization": "Bearer test-token", "Content-Type": "text/plain"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        self.assertEqual(resp.status, 200)
        data = json.loads(resp.read())
        self.assertEqual(data["revision"], 1)

    def test_post_slot_invalid_token(self):
        req = urllib.request.Request(
            f"{self._url}/slot",
            data=b"hello",
            headers={"Authorization": "Bearer wrong"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(exc.exception.code, 401)

    def test_post_slot_missing_auth(self):
        req = urllib.request.Request(
            f"{self._url}/slot", data=b"hello", method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(exc.exception.code, 401)

    def test_get_slot_valid_token(self):
        # First write
        req = urllib.request.Request(
            f"{self._url}/slot", data=b"snapshot-test",
            headers={"Authorization": "Bearer test-token"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        # Then snapshot
        req2 = urllib.request.Request(
            f"{self._url}/slot",
            headers={"Authorization": "Bearer test-token"},
        )
        resp = urllib.request.urlopen(req2, timeout=5)
        data = json.loads(resp.read())
        self.assertEqual(data["value"], "snapshot-test")
        self.assertEqual(data["revision"], 1)

    def test_get_slot_invalid_token(self):
        req = urllib.request.Request(
            f"{self._url}/slot",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(exc.exception.code, 401)

    def test_sse_receives_write_events(self):
        events: list[str] = []

        def sse_reader() -> None:
            try:
                req = urllib.request.Request(
                    f"{self._url}/slot/events",
                    headers={"Authorization": "Bearer test-token", "Accept": "text/event-stream"},
                )
                resp = urllib.request.urlopen(req, timeout=10)
                for line in resp:
                    events.append(line.decode().strip())
                    if len(events) >= 4:
                        break
            except Exception:
                pass  # server shutdown during cleanup

        t = threading.Thread(target=sse_reader, daemon=True)
        t.start()
        time.sleep(0.3)  # let SSE connect
        # POST a write
        req = urllib.request.Request(
            f"{self._url}/slot", data=b"sse-payload",
            headers={"Authorization": "Bearer test-token"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        t.join(timeout=5)
        joined = "\n".join(events)
        self.assertIn("event: write", joined)
        self.assertIn("sse-payload", joined)

    def test_sse_invalid_token_401(self):
        req = urllib.request.Request(
            f"{self._url}/slot/events",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(exc.exception.code, 401)

    def test_empty_tokens_refuses_all(self):
        state = RepeaterState(tokens=[])  # empty = refuse all
        handler = make_handler(state)
        srv = RepeaterServer(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/slot",
                headers={"Authorization": "Bearer anything"},
            )
            with self.assertRaises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(exc.exception.code, 401)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()