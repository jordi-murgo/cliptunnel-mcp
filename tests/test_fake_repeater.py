"""TDD tests for FakeRepeater — the in-process test double for the repeater.

Tests cover:
  * post_slot / get_slot convenience methods
  * HttpClient protocol methods (post, get, open_stream)
  * SSE streaming via FakeSseStream
  * Token validation (hmac.compare_digest)
  * Thread-safe writes
  * Keepalive and stream-drop helpers
"""
from __future__ import annotations

import json
import threading
import time
import unittest

from tests.fake_repeater import FakeRepeater


# ---------------------------------------------------------------------------
# Basic post / get / revision / writes
# ---------------------------------------------------------------------------

class TestFakeRepeaterBasic(unittest.TestCase):
    def test_post_slot_stores_value_and_bumps_revision(self) -> None:
        r = FakeRepeater()
        status, body = r.post_slot("hello", token="t")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["revision"], 1)
        self.assertEqual(r.value, "hello")
        self.assertEqual(r.revision, 1)

    def test_get_slot_returns_current_state(self) -> None:
        r = FakeRepeater()
        r.post_slot("data", token="t")
        status, body = r.get_slot(token="t")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["value"], "data")
        self.assertEqual(data["revision"], 1)

    def test_post_with_bad_token_returns_401(self) -> None:
        r = FakeRepeater(tokens=["good"])
        status, body = r.post_slot("x", token="bad")
        self.assertEqual(status, 401)

    def test_get_with_bad_token_returns_401(self) -> None:
        r = FakeRepeater(tokens=["good"])
        r.post_slot("data", token="good")
        status, body = r.get_slot(token="bad")
        self.assertEqual(status, 401)

    def test_writes_list_records_all_bodies(self) -> None:
        r = FakeRepeater()
        r.post_slot("a", token="t")
        r.post_slot("b", token="t")
        self.assertEqual(r.writes, ["a", "b"])

    def test_revision_starts_at_zero(self) -> None:
        r = FakeRepeater()
        self.assertEqual(r.revision, 0)

    def test_value_starts_empty(self) -> None:
        r = FakeRepeater()
        self.assertEqual(r.value, "")


# ---------------------------------------------------------------------------
# HttpClient protocol conformance
# ---------------------------------------------------------------------------

class TestFakeRepeaterHttpClient(unittest.TestCase):
    def test_post_method_matches_protocol(self) -> None:
        r = FakeRepeater()
        resp = r.post("http://x/slot", {"Authorization": "Bearer t"}, "body", 15.0)
        self.assertEqual(resp.status, 200)
        self.assertIn("revision", resp.body)

    def test_get_method_matches_protocol(self) -> None:
        r = FakeRepeater()
        r.post_slot("val", token="t")
        resp = r.get("http://x/slot", {"Authorization": "Bearer t"}, 15.0)
        self.assertEqual(resp.status, 200)
        self.assertIn("val", resp.body)

    def test_post_bad_token_returns_401_response(self) -> None:
        r = FakeRepeater(tokens=["good"])
        resp = r.post("http://x/slot", {"Authorization": "Bearer bad"}, "body", 15.0)
        self.assertEqual(resp.status, 401)

    def test_get_bad_token_returns_401_response(self) -> None:
        r = FakeRepeater(tokens=["good"])
        resp = r.get("http://x/slot", {"Authorization": "Bearer bad"}, 15.0)
        self.assertEqual(resp.status, 401)

    def test_post_response_body_contains_revision(self) -> None:
        r = FakeRepeater()
        r.post_slot("first", token="t")
        resp = r.post("http://x/slot", {"Authorization": "Bearer t"}, "second", 15.0)
        self.assertEqual(json.loads(resp.body)["revision"], 2)

    def test_get_response_body_contains_value_and_revision(self) -> None:
        r = FakeRepeater()
        r.post_slot("payload", token="t")
        resp = r.get("http://x/slot", {"Authorization": "Bearer t"}, 15.0)
        data = json.loads(resp.body)
        self.assertEqual(data["value"], "payload")
        self.assertEqual(data["revision"], 1)


# ---------------------------------------------------------------------------
# SSE streaming
# ---------------------------------------------------------------------------

class TestFakeRepeaterSSE(unittest.TestCase):
    def test_sse_yields_write_events(self) -> None:
        r = FakeRepeater()
        stream = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        events: list[str] = []
        def reader() -> None:
            for line in stream:
                events.append(line)
                if len(events) >= 4:
                    break
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.1)  # let stream connect
        r.post_slot("payload", token="t")
        t.join(timeout=2)
        joined = "".join(events)
        self.assertIn("event: write", joined)
        self.assertIn("payload", joined)

    def test_sse_with_bad_token_raises(self) -> None:
        r = FakeRepeater(tokens=["good"])
        with self.assertRaises(Exception):
            r.open_stream("http://x/slot/events", {"Authorization": "Bearer bad"})

    def test_sse_event_contains_revision(self) -> None:
        r = FakeRepeater()
        stream = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        events: list[str] = []
        def reader() -> None:
            for line in stream:
                events.append(line)
                if len(events) >= 4:
                    break
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.1)
        r.post_slot("data", token="t")
        t.join(timeout=2)
        joined = "".join(events)
        self.assertIn('"revision": 1', joined)

    def test_sse_keepalive_comment(self) -> None:
        r = FakeRepeater()
        stream = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        events: list[str] = []
        def reader() -> None:
            for line in stream:
                events.append(line)
                if len(events) >= 2:
                    break
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.1)
        r.push_keepalive()
        t.join(timeout=2)
        joined = "".join(events)
        self.assertIn(": keepalive", joined)

    def test_sse_stream_close(self) -> None:
        r = FakeRepeater()
        stream = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        stream.close()
        # After close, iteration should stop without blocking forever
        events: list[str] = []
        def reader() -> None:
            for line in stream:
                events.append(line)
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "stream iterator should terminate after close()")

    def test_drop_all_streams_stops_iterators(self) -> None:
        r = FakeRepeater()
        stream = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        events: list[str] = []
        def reader() -> None:
            for line in stream:
                events.append(line)
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        time.sleep(0.1)
        r.drop_all_streams()
        t.join(timeout=2)
        self.assertFalse(t.is_alive(), "stream iterator should terminate after drop_all_streams()")

    def test_multiple_streams_all_receive_events(self) -> None:
        r = FakeRepeater()
        s1 = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        s2 = r.open_stream("http://x/slot/events", {"Authorization": "Bearer t"})
        events1: list[str] = []
        events2: list[str] = []
        def read1() -> None:
            for line in s1:
                events1.append(line)
                if len(events1) >= 4:
                    break
        def read2() -> None:
            for line in s2:
                events2.append(line)
                if len(events2) >= 4:
                    break
        t1 = threading.Thread(target=read1, daemon=True)
        t2 = threading.Thread(target=read2, daemon=True)
        t1.start()
        t2.start()
        time.sleep(0.1)
        r.post_slot("broadcast", token="t")
        t1.join(timeout=2)
        t2.join(timeout=2)
        self.assertIn("broadcast", "".join(events1))
        self.assertIn("broadcast", "".join(events2))


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

class TestFakeRepeaterThreadSafety(unittest.TestCase):
    def test_concurrent_writes_increment_correctly(self) -> None:
        r = FakeRepeater()

        def writer(n: int) -> None:
            for i in range(50):
                r.post_slot(f"w{n}-{i}", token="t")

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(r.revision, 200)
        self.assertEqual(len(r.writes), 200)


if __name__ == "__main__":
    unittest.main()