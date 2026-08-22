"""Tests for EncryptedTransport — AES-256-GCM decorator over any transport.

TDD: tests written first (RED), then EncryptedTransport implemented (GREEN),
then edge cases (TRIANGULATE).
"""

from __future__ import annotations

import base64
import os
import threading
import time
import unittest

from cliptunnel_mcp.crypto import decrypt, encrypt
from cliptunnel_mcp.encrypted_transport import EncryptedTransport
from tests.clipboard_slot import ClipboardSlot


def _random_key() -> bytes:
    return os.urandom(32)


class _FakeTransport:
    """Minimal fake transport for testing — wraps a ClipboardSlot."""

    def __init__(self) -> None:
        self._slot = ClipboardSlot()

    @property
    def backend_name(self) -> str:
        return "fake"

    def read(self) -> str:
        return self._slot.read()

    def write(self, value: str) -> None:
        self._slot.write(value)

    @property
    def revision(self) -> int:
        return self._slot.revision

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
        return self._slot.wait_for_revision(after, timeout)

    def close(self) -> None:
        pass


class TestProtocolConformance(unittest.TestCase):
    def test_is_transport(self) -> None:
        from cliptunnel_mcp.transport import Transport

        t = EncryptedTransport(_FakeTransport(), _random_key())
        self.assertIsInstance(t, Transport)

    def test_backend_name_prefixed(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        self.assertEqual(t.backend_name, "encrypted:fake")


class TestEncryptDecryptRoundTrip(unittest.TestCase):
    def test_write_then_read_recovers_plaintext(self) -> None:
        inner = _FakeTransport()
        key = _random_key()
        t = EncryptedTransport(inner, key)
        t.write("hello world")
        self.assertEqual(t.read(), "hello world")

    def test_write_encrypts_on_inner(self) -> None:
        inner = _FakeTransport()
        key = _random_key()
        t = EncryptedTransport(inner, key)
        t.write("secret plaintext")
        # Inner holds encrypted blob, not plaintext.
        self.assertNotEqual(inner.read(), "secret plaintext")
        # And it's a valid AES blob that decrypts back.
        self.assertEqual(decrypt(inner.read(), key), "secret plaintext")

    def test_round_trip_unicode(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        plaintext = "hélïpö — 日本語 — 🎉"
        t.write(plaintext)
        self.assertEqual(t.read(), plaintext)

    def test_round_trip_empty(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        t.write("")
        self.assertEqual(t.read(), "")

    def test_multiple_writes(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        t.write("a")
        self.assertEqual(t.read(), "a")
        t.write("b")
        self.assertEqual(t.read(), "b")
        t.write("c")
        self.assertEqual(t.read(), "c")

    def test_revision_bumps_on_write(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        self.assertEqual(t.revision, 0)
        t.write("a")
        self.assertEqual(t.revision, 1)
        t.write("b")
        self.assertEqual(t.revision, 2)


class TestWaitForChange(unittest.TestCase):
    def test_wait_returns_on_write(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        rev = t.revision

        def delayed_write() -> None:
            time.sleep(0.1)
            t.write("delayed")

        threading.Thread(target=delayed_write, daemon=True).start()
        result = t.wait_for_change(rev, timeout=2.0)
        self.assertGreater(result, rev)

    def test_wait_times_out_without_raise(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        result = t.wait_for_change(t.revision, timeout=0.2)
        self.assertEqual(result, t.revision)


class TestDelegation(unittest.TestCase):
    def test_close_delegates_to_inner(self) -> None:
        closed = []

        class CloseableTransport(_FakeTransport):
            def close(self) -> None:
                closed.append(True)

        t = EncryptedTransport(CloseableTransport(), _random_key())
        t.close()
        self.assertEqual(closed, [True])

    def test_close_idempotent(self) -> None:
        t = EncryptedTransport(_FakeTransport(), _random_key())
        t.close()
        t.close()  # no exception

    def test_restore_user_clipboard_delegates(self) -> None:
        restored = []

        class RestorableTransport(_FakeTransport):
            def restore_user_clipboard(self) -> bool:
                restored.append(True)
                return True

        t = EncryptedTransport(RestorableTransport(), _random_key())
        self.assertTrue(t.restore_user_clipboard())
        self.assertEqual(restored, [True])


class TestWithClipboardTransport(unittest.TestCase):
    """Integration: EncryptedTransport works over a real ClipboardTransport-like slot."""

    def test_encrypted_clipboard_round_trip(self) -> None:
        inner = _FakeTransport()
        key = _random_key()
        t = EncryptedTransport(inner, key)
        t.write("clipboard-encrypted-payload")
        self.assertEqual(t.read(), "clipboard-encrypted-payload")
        # The slot holds an encrypted blob, not plaintext.
        self.assertNotEqual(inner.read(), "clipboard-encrypted-payload")


if __name__ == "__main__":
    unittest.main()