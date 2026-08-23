"""TDD tests for FirebaseTransport — Transport + RevisionMonitor over the
Firebase Realtime Database REST API.

Uses FakeFirebase as the HttpClient double so no network I/O is needed.
Compatible with both pytest and unittest discover.
"""
from __future__ import annotations

import json
import os
import threading
import unittest

from cliptunnel_mcp.encrypted_transport import EncryptedTransport
from cliptunnel_mcp.firebase_transport import FirebaseTransport
from cliptunnel_mcp.https_transport import (
    HttpResponse,
    TransportAuthError,
    TransportError,
)
from cliptunnel_mcp.transport import RevisionMonitor, Transport
from tests.fake_firebase import FakeFirebase

_DB = "https://fake-default-rtdb.firebaseio.com"
_TOKEN = "t"


def make_transport(**overrides):
    """Build a FirebaseTransport backed by a FakeFirebase."""
    fake = FakeFirebase(token=_TOKEN)
    params = {
        "database_url": _DB,
        "auth_token": _TOKEN,
        "http_client": fake,
        "sse_reconnect_delay": 0.05,
    }
    params.update(overrides)
    t = FirebaseTransport(**params)
    return t, fake


def remote_write(fake: FakeFirebase, value: str) -> None:
    """Simulate the REMOTE side writing through Firebase."""
    fake.put(
        f"{_DB}/cliptunnel.json?auth={_TOKEN}",
        {"Authorization": f"Bearer {_TOKEN}"},
        json.dumps({"v": value, "r": {".sv": "timestamp"}}),
        15.0,
    )


def _wait_until(predicate, timeout: float = 5.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


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
        self.assertEqual(t.backend_name, "firebase")
        t.close()


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

class TestReadWrite(unittest.TestCase):
    def test_write_returns_node_and_bumps_revision(self) -> None:
        t, _ = make_transport()
        try:
            t.write("hello")
            self.assertEqual(t.read(), "hello")
            self.assertGreater(t.revision, 0)
        finally:
            t.close()

    def test_write_revision_monotonic(self) -> None:
        t, _ = make_transport()
        try:
            self.assertEqual(t.revision, 0)
            t.write("one")
            r1 = t.revision
            t.write("two")
            r2 = t.revision
            self.assertGreater(r2, r1)
            self.assertEqual(t.read(), "two")
        finally:
            t.close()

    def test_write_wire_format(self) -> None:
        """PUT must target <url>/<node>.json?auth=<token> with a node body
        whose r is the {".sv": "timestamp"} server placeholder."""
        t, fake = make_transport()
        try:
            t.write("hello")
            self.assertEqual(len(fake.puts), 1)
            url, body = fake.puts[0]
            self.assertIn("/cliptunnel.json", url)
            self.assertIn("auth=", url)
            self.assertEqual(body["v"], "hello")
            self.assertEqual(body["r"], {".sv": "timestamp"})
        finally:
            t.close()

    def test_custom_node_path_in_url(self) -> None:
        t, fake = make_transport(node_path="customslot")
        try:
            t.write("hello")
            url, _ = fake.puts[0]
            self.assertIn("/customslot.json", url)
            self.assertNotIn("/cliptunnel.json", url)
        finally:
            t.close()

    def test_trailing_slash_database_url_normalized(self) -> None:
        t, fake = make_transport(database_url=_DB + "/")
        try:
            t.write("hello")
            url, _ = fake.puts[0]
            self.assertNotIn("//", url.replace("https://", ""))
            self.assertIn("/cliptunnel.json", url)
        finally:
            t.close()


# ---------------------------------------------------------------------------
# RevisionMonitor
# ---------------------------------------------------------------------------

class TestWaitForChange(unittest.TestCase):
    def test_wait_timeout_returns_current_revision(self) -> None:
        """wait_for_change never raises on timeout — bounded wait."""
        t, _ = make_transport()
        try:
            rev = t.revision
            out = t.wait_for_change(rev, timeout=0.2)
            self.assertEqual(out, rev)
        finally:
            t.close()

    def test_wait_wakes_on_remote_sse_write(self) -> None:
        t, fake = make_transport()
        try:
            self.assertTrue(_wait_until(lambda: fake.has_streams))
            before = t.revision
            result: dict = {}

            def _waiter() -> None:
                result["rev"] = t.wait_for_change(before, timeout=5.0)

            th = threading.Thread(target=_waiter, daemon=True)
            th.start()
            remote_write(fake, "wake-up")
            th.join(timeout=5.0)
            self.assertFalse(th.is_alive())
            self.assertGreater(result["rev"], before)
        finally:
            t.close()


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

class TestSSE(unittest.TestCase):
    def test_initial_event_populates_cache(self) -> None:
        """A node that exists before connecting arrives via the initial
        SSE put event — no explicit snapshot call needed."""
        fake = FakeFirebase(token=_TOKEN)
        remote_write(fake, "preset")
        t = FirebaseTransport(
            database_url=_DB,
            auth_token=_TOKEN,
            http_client=fake,
            sse_reconnect_delay=0.05,
        )
        try:
            self.assertTrue(_wait_until(lambda: t.read() == "preset"))
            self.assertEqual(t.revision, fake.revision)
        finally:
            t.close()

    def test_remote_write_updates_cache_via_sse(self) -> None:
        t, fake = make_transport()
        try:
            self.assertTrue(_wait_until(lambda: fake.has_streams))
            remote_write(fake, "from-remote")
            self.assertTrue(_wait_until(lambda: t.read() == "from-remote"))
            self.assertEqual(t.revision, fake.revision)
        finally:
            t.close()

    def test_snapshot_resync_after_stream_drop(self) -> None:
        """A write landing while the stream is down is picked up by the
        snapshot resync on reconnect."""
        t, fake = make_transport()
        try:
            t.write("first")
            self.assertTrue(_wait_until(lambda: fake.has_streams))
            fake.drop_all_streams()
            # Remote write lands while we are disconnected from the stream.
            remote_write(fake, "while-away")
            self.assertTrue(_wait_until(lambda: t.read() == "while-away"))
            self.assertEqual(t.revision, fake.revision)
        finally:
            t.close()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class _BrokenClient:
    """HttpClient double whose every call fails with HTTP 500."""

    def put(self, url, headers, body, timeout):
        return HttpResponse(500, "boom")

    def post(self, url, headers, body, timeout):
        return HttpResponse(500, "boom")

    def get(self, url, headers, timeout):
        return HttpResponse(500, "boom")

    def open_stream(self, url, headers):
        raise TransportError("unreachable")


class TestAuth(unittest.TestCase):
    def test_write_auth_error(self) -> None:
        fake = FakeFirebase(token="right")
        t = FirebaseTransport(
            database_url=_DB,
            auth_token="wrong",
            http_client=fake,
            sse_reconnect_delay=0.05,
        )
        try:
            with self.assertRaises(TransportAuthError):
                t.write("nope")
        finally:
            t.close()


class TestErrors(unittest.TestCase):
    def test_unknown_status_raises_transport_error(self) -> None:
        t = FirebaseTransport(
            database_url=_DB,
            auth_token=_TOKEN,
            http_client=_BrokenClient(),
            sse_reconnect_delay=0.05,
        )
        try:
            with self.assertRaises(TransportError):
                t.write("x")
        finally:
            t.close()


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestClose(unittest.TestCase):
    def test_close_stops_sse_thread(self) -> None:
        t, _ = make_transport()
        t.close()
        self.assertFalse(t._sse_thread.is_alive())

    def test_close_idempotent(self) -> None:
        t, _ = make_transport()
        try:
            t.write("x")
        finally:
            t.close()
        t.close()  # second close must not raise
        self.assertFalse(t._sse_thread.is_alive())


# ---------------------------------------------------------------------------
# AES composition
# ---------------------------------------------------------------------------

class TestAESComposition(unittest.TestCase):
    def test_encrypted_round_trip(self) -> None:
        """EncryptedTransport(FirebaseTransport(...)) stores ciphertext on
        the wire and round-trips plaintext through read()."""
        inner, fake = make_transport()
        et = EncryptedTransport(inner, os.urandom(32))
        try:
            et.write("secret-payload")
            self.assertNotEqual(fake.value, "secret-payload")
            self.assertNotIn("secret-payload", fake.value)
            self.assertEqual(et.read(), "secret-payload")
            self.assertEqual(et.backend_name, "encrypted:firebase")
        finally:
            et.close()


if __name__ == "__main__":
    unittest.main()
