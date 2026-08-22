"""Tests for the repeater service — T5 (RED first).

RepeaterState: slot_value, revision, subscribers, threading.Lock, tokens.
make_handler(state) → BaseHTTPRequestHandler subclass.
do_GET routes /slot (snapshot JSON) and /slot/events (SSE stream).
do_POST routes /slot (write). Bearer auth. RepeaterServer(ThreadingHTTPServer).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from cliptunnel_mcp.repeater.state import RepeaterState


# ---------------------------------------------------------------------------
# RepeaterState unit tests
# ---------------------------------------------------------------------------


class TestRepeaterState:
    def test_write_stores_value_and_bumps_revision(self):
        s = RepeaterState()
        rev = s.write("hello")
        assert rev == 1
        assert s.value == "hello"
        assert s.revision == 1

    def test_snapshot_returns_value_and_revision(self):
        s = RepeaterState()
        s.write("data")
        val, rev = s.snapshot()
        assert val == "data"
        assert rev == 1

    def test_multiple_writes_increment(self):
        s = RepeaterState()
        s.write("a")
        s.write("b")
        s.write("c")
        assert s.revision == 3

    def test_add_remove_subscriber(self):
        s = RepeaterState()
        q = s.add_subscriber()
        assert q is not None
        s.remove_subscriber(q)

    def test_write_pushes_to_subscribers(self):
        s = RepeaterState()
        q = s.add_subscriber()
        s.write("pushed")
        event = q.get(timeout=1.0)
        assert "write" in event
        assert "pushed" in event

    def test_full_queue_drops_event(self):
        s = RepeaterState(maxsize=2)
        q = s.add_subscriber()
        s.write("a")
        s.write("b")
        # Queue should be full; next write drops — must not block.
        s.write("c")
        # The queue has at most 2 items.
        assert q.qsize() <= 2

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
        assert s.revision == 200  # 4 * 50


# ---------------------------------------------------------------------------
# HTTP handler tests (in-process ThreadingHTTPServer)
# ---------------------------------------------------------------------------

from cliptunnel_mcp.repeater.server import make_handler, RepeaterServer  # noqa: E402


class TestRepeaterHTTP:
    @pytest.fixture
    def server(self):
        state = RepeaterState(tokens=["test-token"])
        handler = make_handler(state)
        srv = RepeaterServer(("127.0.0.1", 0), handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield f"http://127.0.0.1:{port}"
        srv.shutdown()

    def test_post_slot_valid_token(self, server):
        req = urllib.request.Request(
            f"{server}/slot",
            data=b"hello",
            headers={"Authorization": "Bearer test-token", "Content-Type": "text/plain"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=5)
        assert resp.status == 200
        data = json.loads(resp.read())
        assert data["revision"] == 1

    def test_post_slot_invalid_token(self, server):
        req = urllib.request.Request(
            f"{server}/slot",
            data=b"hello",
            headers={"Authorization": "Bearer wrong"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401

    def test_post_slot_missing_auth(self, server):
        req = urllib.request.Request(
            f"{server}/slot", data=b"hello", method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401

    def test_get_slot_valid_token(self, server):
        # First write
        req = urllib.request.Request(
            f"{server}/slot", data=b"snapshot-test",
            headers={"Authorization": "Bearer test-token"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        # Then snapshot
        req2 = urllib.request.Request(
            f"{server}/slot",
            headers={"Authorization": "Bearer test-token"},
        )
        resp = urllib.request.urlopen(req2, timeout=5)
        data = json.loads(resp.read())
        assert data["value"] == "snapshot-test"
        assert data["revision"] == 1

    def test_get_slot_invalid_token(self, server):
        req = urllib.request.Request(
            f"{server}/slot",
            headers={"Authorization": "Bearer wrong"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401

    def test_sse_receives_write_events(self, server):
        events: list[str] = []

        def sse_reader() -> None:
            try:
                req = urllib.request.Request(
                    f"{server}/slot/events",
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
            f"{server}/slot", data=b"sse-payload",
            headers={"Authorization": "Bearer test-token"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
        t.join(timeout=5)
        joined = "\n".join(events)
        assert "event: write" in joined
        assert "sse-payload" in joined

    def test_sse_invalid_token_401(self, server):
        req = urllib.request.Request(
            f"{server}/slot/events",
            headers={"Authorization": "Bearer wrong"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=5)
        assert exc.value.code == 401

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
            with pytest.raises(urllib.error.HTTPError) as exc:
                urllib.request.urlopen(req, timeout=5)
            assert exc.value.code == 401
        finally:
            srv.shutdown()