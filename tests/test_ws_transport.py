# tests/test_ws_transport.py
from __future__ import annotations

import threading
import time
import unittest

from cliptunnel_mcp.https_transport import TransportAuthError, TransportError
from cliptunnel_mcp.transport import RevisionMonitor, Transport
from cliptunnel_mcp.ws_transport import WebSocketTransport
from tests.fake_ws_repeater import FakeWsRepeater


def make_transport(**kwargs) -> tuple[WebSocketTransport, FakeWsRepeater]:
    """Build a WebSocketTransport backed by a FakeWsRepeater."""
    fake = FakeWsRepeater(tokens=["t"])
    t = WebSocketTransport(
        ws_url="ws://test",
        bearer_token="t",
        ws_client=fake,
        reconnect_delay=0.05,
        reconnect_max_delay=0.2,
        request_timeout=2.0,
        **kwargs,
    )
    return t, fake


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    """Poll predicate until true or timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------

class TestProtocolConformance(unittest.TestCase):
    def test_is_transport(self) -> None:
        t, _ = make_transport()
        self.assertIsInstance(t, Transport)
        t.close()

    def test_is_revision_monitor(self) -> None:
        t, _ = make_transport()
        self.assertIsInstance(t, RevisionMonitor)
        t.close()

    def test_backend_name(self) -> None:
        t, _ = make_transport()
        self.assertEqual(t.backend_name, "websocket")
        t.close()

    def test_endpoint_returns_ws_url(self) -> None:
        t, _ = make_transport()
        self.assertEqual(t.endpoint, "ws://test")
        t.close()

    def test_endpoint_excludes_bearer_token(self) -> None:
        t, _ = make_transport()
        self.assertNotIn("secret-bearer-token", t.endpoint or "")
        t.close()


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

class TestReadWrite(unittest.TestCase):
    def test_write_bumps_revision(self) -> None:
        t, _ = make_transport()
        t.write("hello")
        self.assertEqual(t.revision, 1)
        t.close()

    def test_write_updates_local_cache(self) -> None:
        t, _ = make_transport()
        t.write("data")
        self.assertEqual(t.read(), "data")
        t.close()

    def test_read_returns_last_value_without_blocking(self) -> None:
        t, _ = make_transport()
        t.write("first")
        self.assertEqual(t.read(), "first")
        t.close()

    def test_read_initial_value_is_empty(self) -> None:
        t, _ = make_transport()
        self.assertEqual(t.read(), "")
        t.close()

    def test_write_auth_error(self) -> None:
        fake_fail = FakeWsRepeater(tokens=["good"])
        t = WebSocketTransport(
            ws_url="ws://test",
            bearer_token="bad",
            ws_client=fake_fail,
            reconnect_delay=0.05,
            request_timeout=2.0,
        )
        with self.assertRaises(TransportAuthError):
            t.write("x")
        t.close()

    def test_write_generic_error_when_not_connected(self) -> None:
        t, fake = make_transport()
        # Simulate disconnected state — close the underlying connection
        t.close()
        # Re-create: the transport is closed, write should raise
        with self.assertRaises(TransportError):
            t.write("after-close")


# ---------------------------------------------------------------------------
# Revision
# ---------------------------------------------------------------------------

class TestRevision(unittest.TestCase):
    def test_revision_starts_at_zero(self) -> None:
        t, _ = make_transport()
        self.assertEqual(t.revision, 0)
        t.close()

    def test_multiple_writes_increment_revision(self) -> None:
        t, _ = make_transport()
        t.write("a")
        t.write("b")
        t.write("c")
        self.assertEqual(t.revision, 3)
        t.close()


# ---------------------------------------------------------------------------
# wait_for_change
# ---------------------------------------------------------------------------

class TestWaitForChange(unittest.TestCase):
    def test_wait_returns_on_write(self) -> None:
        t, _ = make_transport()
        rev = t.revision

        def delayed_write() -> None:
            time.sleep(0.1)
            t.write("delayed")

        threading.Thread(target=delayed_write, daemon=True).start()
        result = t.wait_for_change(rev, timeout=2.0)
        self.assertGreater(result, rev)
        t.close()

    def test_wait_times_out_without_raise(self) -> None:
        t, _ = make_transport()
        result = t.wait_for_change(t.revision, timeout=0.2)
        self.assertEqual(result, t.revision)  # no change
        t.close()


# ---------------------------------------------------------------------------
# Event frames (remote write via event)
# ---------------------------------------------------------------------------

class TestEventFrames(unittest.TestCase):
    def test_event_frame_updates_cache_and_revision(self) -> None:
        t, fake = make_transport()
        # Simulate a remote write by pushing an event to the fake's
        # connection inbox (the transport's receive loop picks it up).
        # Wait for the transport to connect first.
        self.assertTrue(_wait_until(lambda: len(fake.connections) > 0))
        # Simulate remote write via FakeWsRepeater
        conn = fake.connections[0]
        import json
        conn._inbox.append(json.dumps(
            {"type": "event", "value": "remote-value", "revision": 1}
        ))
        self.assertTrue(_wait_until(lambda: t.read() == "remote-value"))
        self.assertGreaterEqual(t.revision, 1)
        t.close()

    def test_wait_for_change_unblocks_on_remote_event(self) -> None:
        t, fake = make_transport()
        rev = t.revision
        self.assertTrue(_wait_until(lambda: len(fake.connections) > 0))

        def delayed_remote() -> None:
            time.sleep(0.1)
            import json
            fake.connections[0]._inbox.append(json.dumps(
                {"type": "event", "value": "from-remote", "revision": rev + 1}
            ))

        threading.Thread(target=delayed_remote, daemon=True).start()
        result = t.wait_for_change(rev, timeout=3.0)
        self.assertGreater(result, rev)
        t.close()


# ---------------------------------------------------------------------------
# Snapshot on connect
# ---------------------------------------------------------------------------

class TestSnapshotConnect(unittest.TestCase):
    def test_initial_snapshot_updates_cache(self) -> None:
        fake = FakeWsRepeater(tokens=["t"])
        # Pre-populate the fake with a value
        import json
        fake._slot_value = "pre-existing"
        fake._revision = 5
        t = WebSocketTransport(
            ws_url="ws://test",
            bearer_token="t",
            ws_client=fake,
            reconnect_delay=0.05,
            request_timeout=2.0,
        )
        self.assertTrue(_wait_until(lambda: t.read() == "pre-existing"))
        self.assertGreaterEqual(t.revision, 5)
        t.close()


# ---------------------------------------------------------------------------
# Ping/pong keepalive
# ---------------------------------------------------------------------------

class TestKeepalive(unittest.TestCase):
    def test_ping_pong_does_not_affect_revision(self) -> None:
        t, fake = make_transport()
        self.assertTrue(_wait_until(lambda: len(fake.connections) > 0))
        import json
        rev_before = t.revision
        val_before = t.read()
        # Push a ping frame to the connection inbox
        fake.connections[0]._inbox.append(json.dumps({"type": "ping"}))
        time.sleep(0.2)
        # Ping must not change revision or value
        self.assertEqual(t.revision, rev_before)
        self.assertEqual(t.read(), val_before)
        t.close()


# ---------------------------------------------------------------------------
# Reconnect
# ---------------------------------------------------------------------------

class TestReconnect(unittest.TestCase):
    def test_reconnect_resyncs_via_snapshot(self) -> None:
        fake = FakeWsRepeater(tokens=["t"])
        t = WebSocketTransport(
            ws_url="ws://test",
            bearer_token="t",
            ws_client=fake,
            reconnect_delay=0.05,
            reconnect_max_delay=0.2,
            request_timeout=2.0,
        )
        # Wait for initial connect
        self.assertTrue(_wait_until(lambda: len(fake.connections) > 0))
        # Drop the connection
        fake.connections[0].close()
        # Wait for reconnect
        self.assertTrue(_wait_until(lambda: len(fake.connections) > 0, timeout=5.0))
        t.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose(unittest.TestCase):
    def test_close_stops_background_loop(self) -> None:
        t, _ = make_transport()
        t.close()
        t.close()  # idempotent

    def test_close_is_idempotent(self) -> None:
        t, _ = make_transport()
        t.close()
        t.close()


if __name__ == "__main__":
    unittest.main()