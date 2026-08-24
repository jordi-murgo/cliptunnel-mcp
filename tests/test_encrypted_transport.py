"""Tests for EncryptedTransport — now a thin pass-through decorator.

Encryption is handled at the protocol level (pack/unpack with aes_key).
EncryptedTransport is kept for backward compatibility but delegates
read/write to the inner transport with no encryption.
"""

from __future__ import annotations

import threading
import time
import unittest

from cliptunnel_mcp.encrypted_transport import EncryptedTransport
from tests.clipboard_slot import ClipboardSlot


class _FakeTransport:
    """Minimal fake transport for testing — wraps a ClipboardSlot."""

    def __init__(self) -> None:
        self._slot = ClipboardSlot()

    @property
    def backend_name(self) -> str:
        return "fake"

    @property
    def endpoint(self) -> str | None:
        return None

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

        t = EncryptedTransport(_FakeTransport())
        self.assertIsInstance(t, Transport)

    def test_backend_name_prefixed(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        self.assertEqual(t.backend_name, "passthrough:fake")


class TestPassThroughReadWrite(unittest.TestCase):
    def test_write_then_read_returns_plaintext(self) -> None:
        inner = _FakeTransport()
        t = EncryptedTransport(inner)
        t.write("hello world")
        self.assertEqual(t.read(), "hello world")

    def test_inner_holds_plaintext(self) -> None:
        inner = _FakeTransport()
        t = EncryptedTransport(inner)
        t.write("plaintext data")
        # Pass-through: inner holds exactly what was written
        self.assertEqual(inner.read(), "plaintext data")

    def test_round_trip_unicode(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        plaintext = "hélïpö — 日本語 — 🎉"
        t.write(plaintext)
        self.assertEqual(t.read(), plaintext)

    def test_round_trip_empty(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        t.write("")
        self.assertEqual(t.read(), "")

    def test_multiple_writes(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        t.write("a")
        self.assertEqual(t.read(), "a")
        t.write("b")
        self.assertEqual(t.read(), "b")


class TestRevision(unittest.TestCase):
    def test_revision_bumps_on_write(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        self.assertEqual(t.revision, 0)
        t.write("a")
        self.assertEqual(t.revision, 1)
        t.write("b")
        self.assertEqual(t.revision, 2)


class TestWaitForChange(unittest.TestCase):
    def test_wait_returns_on_write(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        rev = t.revision

        def delayed_write() -> None:
            time.sleep(0.1)
            t.write("delayed")

        threading.Thread(target=delayed_write, daemon=True).start()
        result = t.wait_for_change(rev, timeout=2.0)
        self.assertGreater(result, rev)

    def test_wait_times_out_without_raise(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        result = t.wait_for_change(t.revision, timeout=0.2)
        self.assertEqual(result, t.revision)


class TestDelegation(unittest.TestCase):
    def test_close_delegates_to_inner(self) -> None:
        closed: list[bool] = []

        class CloseableTransport:
            backend_name = "closeable"
            endpoint = None
            revision = 0

            def read(self) -> str:
                return ""

            def write(self, value: str) -> None:
                pass

            def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
                return 0

            def close(self) -> None:
                closed.append(True)

        t = EncryptedTransport(CloseableTransport())
        t.close()
        self.assertEqual(closed, [True])

    def test_close_idempotent(self) -> None:
        t = EncryptedTransport(_FakeTransport())
        t.close()
        t.close()  # no exception

    def test_restore_delegates_to_inner(self) -> None:
        restored: list[bool] = []

        class RestorableTransport:
            backend_name = "restorable"
            endpoint = None
            revision = 0

            def read(self) -> str:
                return ""

            def write(self, value: str) -> None:
                pass

            def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
                return 0

            def close(self) -> None:
                pass

            def restore_user_clipboard(self) -> bool:
                restored.append(True)
                return True

        t = EncryptedTransport(RestorableTransport())
        self.assertTrue(t.restore_user_clipboard())
        self.assertEqual(restored, [True])


class TestWithClipboardTransport(unittest.TestCase):
    """Integration: EncryptedTransport passes through over a ClipboardSlot."""

    def test_passthrough_clipboard_round_trip(self) -> None:
        inner = _FakeTransport()
        t = EncryptedTransport(inner)
        t.write("clipboard-payload")
        self.assertEqual(t.read(), "clipboard-payload")
        # The slot holds exactly what was written (no encryption)
        self.assertEqual(inner.read(), "clipboard-payload")


if __name__ == "__main__":
    unittest.main()