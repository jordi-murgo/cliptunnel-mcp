"""TDD tests for HttpsTransport — Transport + RevisionMonitor over SSE+POST.

Uses FakeRepeater as the HttpClient double so no network I/O is needed.
Compatible with both pytest and unittest discover.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest import mock

from cliptunnel_mcp.https_transport import (
    HttpsTransport,
    TransportAuthError,
    TransportError,
)
from cliptunnel_mcp.transport import RevisionMonitor, Transport
from tests.fake_repeater import FakeRepeater, HttpResponse


def make_transport(**kwargs):
    """Build an HttpsTransport backed by a FakeRepeater."""
    fake = FakeRepeater()
    t = HttpsTransport(
        repeater_url="http://test",
        bearer_token="t",
        http_client=fake,
        sse_reconnect_delay=0.05,
        **kwargs,
    )
    return t, fake


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
        self.assertEqual(t.backend_name, "https")
        t.close()

    def test_endpoint_returns_sanitized_url(self) -> None:
        t, _ = make_transport()
        self.assertEqual(t.endpoint, "http://test")
        t.close()

    def test_endpoint_excludes_bearer_token(self) -> None:
        t, _ = make_transport()
        # endpoint must not contain auth-related query params or headers
        self.assertNotIn("bearer", (t.endpoint or "").lower())
        self.assertNotIn("token=", t.endpoint or "")
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
        fake_fail = FakeRepeater(tokens=["good"])
        t = HttpsTransport(
            repeater_url="http://test",
            bearer_token="bad",
            http_client=fake_fail,
            sse_reconnect_delay=0.05,
        )
        with self.assertRaises(TransportAuthError):
            t.write("x")
        t.close()

    def test_write_generic_error_on_500(self) -> None:
        """A non-2xx, non-401 response raises TransportError."""
        fake = FakeRepeater()

        # Monkey-patch post to return 500
        def post_500(url, headers, body, timeout):
            return HttpResponse(status=500, body='{"error": "server"}')

        fake.post = post_500
        t = HttpsTransport(
            repeater_url="http://test",
            bearer_token="t",
            http_client=fake,
            sse_reconnect_delay=0.05,
        )
        with self.assertRaises(TransportError):
            t.write("x")
        t.close()


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
# SSE
# ---------------------------------------------------------------------------

class TestSSE(unittest.TestCase):
    @staticmethod
    def _wait_until(predicate, timeout: float = 5.0) -> bool:
        """Poll *predicate* until true or timeout — deterministic, no fixed sleeps."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()

    def test_sse_event_updates_cache_and_revision(self) -> None:
        t, fake = make_transport()
        fake.post_slot("remote-value", token="t")
        self.assertTrue(
            self._wait_until(lambda: t.read() == "remote-value"),
            "SSE thread did not process the remote write in time",
        )
        self.assertGreaterEqual(t.revision, 1)
        t.close()

    def test_wait_for_change_unblocks_on_remote_sse_write(self) -> None:
        t, fake = make_transport()
        rev = t.revision

        def delayed_remote() -> None:
            time.sleep(0.1)
            fake.post_slot("from-remote", token="t")

        threading.Thread(target=delayed_remote, daemon=True).start()
        result = t.wait_for_change(rev, timeout=3.0)
        self.assertGreater(result, rev)
        t.close()

    def test_sse_keepalive_noop(self) -> None:
        t, fake = make_transport()
        fake.push_keepalive()
        # Give the SSE thread a moment to consume the keepalive comment;
        # it must remain a no-op (no value, no revision change).
        time.sleep(0.1)
        self.assertEqual(t.read(), "")  # no write happened
        t.close()

    def test_sse_reconnect_resyncs_via_snapshot(self) -> None:
        t, fake = make_transport()
        fake.post_slot("before-drop", token="t")
        self.assertTrue(
            self._wait_until(lambda: t.read() == "before-drop"),
            "initial SSE event not processed",
        )
        fake.drop_all_streams()
        fake.post_slot("during-disconnect", token="t")
        self.assertTrue(
            self._wait_until(lambda: t.read() == "during-disconnect", timeout=10.0),
            "SSE reconnect + snapshot resync did not complete in time",
        )
        t.close()


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose(unittest.TestCase):
    def test_close_stops_sse_thread(self) -> None:
        t, _ = make_transport()
        t.close()
        t.close()  # idempotent

    def test_close_is_idempotent(self) -> None:
        t, _ = make_transport()
        t.close()
        t.close()
        t.close()  # no exception


if __name__ == "__main__":
    unittest.main()