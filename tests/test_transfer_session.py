"""Unit tests for TransferSessionManager (src/cliptunnel_mcp/transfer_session.py).

Tests the full session lifecycle: creation, block append ordering, checksum
verification, atomic rename, cancel cleanup, timeout sweep, and concurrent
sessions. Uses tempfile.TemporaryDirectory so nothing touches the real
filesystem.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest

from cliptunnel_mcp.transfer_session import TransferSessionManager


class TestTransferSessionManager(unittest.TestCase):
    """Direct unit tests for TransferSessionManager."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._manager: TransferSessionManager | None = None

    def tearDown(self) -> None:
        if self._manager is not None:
            self._manager.close()

    # ── (a) create_session upload ──────────────────────────────────────

    def test_create_session_upload(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "upload.bin")
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc123",
        )
        self.assertIsInstance(tid, str)
        self.assertTrue(len(tid) > 0)
        self.assertEqual(total_blocks, 16)
        session = self._manager.get_session(tid)
        self.assertIsNotNone(session)
        assert session is not None  # for type narrowing
        self.assertEqual(session.direction, "upload")
        self.assertEqual(session.filename, filename)
        self.assertEqual(session.block_size, 65536)
        self.assertEqual(session.total_blocks, 16)
        self.assertEqual(session.expected_checksum, "abc123")
        self.assertEqual(session.received_blocks, 0)
        self.assertIsNotNone(session.temp_path)

    # ── (b) create_session download ────────────────────────────────────

    def test_create_session_download(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "download.bin")
        with open(filename, "wb") as f:
            f.write(b"\x00" * 1048576)
        tid, total_blocks = self._manager.create_session(
            "download", filename, size=1048576, block_size=65536, checksum="abc123",
        )
        self.assertIsInstance(tid, str)
        self.assertEqual(total_blocks, 16)
        session = self._manager.get_session(tid)
        self.assertIsNotNone(session)
        assert session is not None
        self.assertEqual(session.direction, "download")

    def test_create_session_download_nonexistent_file(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "no_such_file.bin")
        with self.assertRaises(ValueError) as ctx:
            self._manager.create_session(
                "download", filename, size=1048576, block_size=65536, checksum="abc",
            )
        self.assertIn("not found", str(ctx.exception))

    # ── (c) total_blocks ceiling division ──────────────────────────────

    def test_total_blocks_ceiling_division(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "odd.bin")
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=65537, block_size=65536, checksum="abc",
        )
        self.assertEqual(total_blocks, 2)

    # ── (d) append_block upload ────────────────────────────────────────

    def test_append_block_upload(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "upload.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data")
        session = self._manager.get_session(tid)
        assert session is not None
        self.assertEqual(session.received_blocks, 1)
        # Temp file grew by 4 bytes
        self.assertEqual(os.path.getsize(session.temp_path), 4)

    # ── (e) append_block out of order ──────────────────────────────────

    def test_append_block_out_of_order(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "upload.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=5 * 65536, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data")
        with self.assertRaises(ValueError) as ctx:
            self._manager.append_block(tid, 3, b"more")
        self.assertIn("out of order", str(ctx.exception))

    # ── (f) append_block duplicate ─────────────────────────────────────

    def test_append_block_duplicate(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "upload.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data")
        with self.assertRaises(ValueError) as ctx:
            self._manager.append_block(tid, 0, b"data")
        self.assertIn("duplicate", str(ctx.exception))

    # ── (g) append_block exceeds total ─────────────────────────────────

    def test_append_block_exceeds_total(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "small.bin")
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=65536, block_size=65536, checksum="abc",
        )
        self.assertEqual(total_blocks, 1)
        with self.assertRaises(ValueError) as ctx:
            self._manager.append_block(tid, 1, b"data")
        self.assertIn("exceeds total_blocks", str(ctx.exception))

    # ── (h) read_block download ────────────────────────────────────────

    def test_read_block_download(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "download.bin")
        content = b"A" * 65536 + b"B" * 100
        with open(filename, "wb") as f:
            f.write(content)
        tid, total_blocks = self._manager.create_session(
            "download", filename, size=len(content), block_size=65536, checksum="abc",
        )
        self.assertEqual(total_blocks, 2)
        block0 = self._manager.read_block(tid, 0)
        self.assertEqual(block0, b"A" * 65536)
        block1 = self._manager.read_block(tid, 1)
        self.assertEqual(block1, b"B" * 100)

    # ── (i) finalize_upload matching checksum ──────────────────────────

    def test_finalize_upload_matching_checksum(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "final.bin")
        data = b"hello world"
        checksum = hashlib.sha256(data).hexdigest()
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=len(data), block_size=65536, checksum=checksum,
        )
        self.assertEqual(total_blocks, 1)
        self._manager.append_block(tid, 0, data)
        path, size, verified = self._manager.finalize_upload(tid)
        self.assertEqual(path, filename)
        self.assertEqual(size, len(data))
        self.assertTrue(verified)
        # Final file exists with correct content
        self.assertTrue(os.path.isfile(filename))
        with open(filename, "rb") as f:
            self.assertEqual(f.read(), data)
        # Temp path no longer exists
        session_after = self._manager.get_session(tid)
        self.assertIsNone(session_after)

    # ── (j) finalize_upload checksum mismatch ──────────────────────────

    def test_finalize_upload_checksum_mismatch(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "mismatch.bin")
        data = b"actual data"
        wrong_checksum = hashlib.sha256(b"different data").hexdigest()
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=len(data), block_size=65536, checksum=wrong_checksum,
        )
        self.assertEqual(total_blocks, 1)
        self._manager.append_block(tid, 0, data)
        path, size, verified = self._manager.finalize_upload(tid)
        self.assertFalse(verified)
        # Final path does NOT exist
        self.assertFalse(os.path.exists(filename))
        # Temp file retained (path is temp_path, not final)
        self.assertTrue(os.path.exists(path))

    # ── (k) finalize_upload incomplete ─────────────────────────────────

    def test_finalize_upload_incomplete(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "incomplete.bin")
        tid, total_blocks = self._manager.create_session(
            "upload", filename, size=131072, block_size=65536, checksum="abc",
        )
        self.assertEqual(total_blocks, 2)
        self._manager.append_block(tid, 0, b"\x00" * 65536)
        with self.assertRaises(ValueError) as ctx:
            self._manager.finalize_upload(tid)
        self.assertIn("not all blocks", str(ctx.exception))

    # ── (l) finalize_download ──────────────────────────────────────────

    def test_finalize_download(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "dl.bin")
        content = b"download me"
        with open(filename, "wb") as f:
            f.write(content)
        tid, _ = self._manager.create_session(
            "download", filename, size=len(content), block_size=65536, checksum="abc",
        )
        path, size = self._manager.finalize_download(tid)
        self.assertEqual(path, filename)
        self.assertEqual(size, len(content))
        # Session cleaned up
        self.assertIsNone(self._manager.get_session(tid))

    # ── (m) cancel upload ──────────────────────────────────────────────

    def test_cancel_upload(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "cancel_up.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data1")
        self._manager.append_block(tid, 1, b"data2")
        session = self._manager.get_session(tid)
        assert session is not None
        temp_path = session.temp_path
        self.assertTrue(os.path.exists(temp_path))
        result = self._manager.cancel(tid)
        self.assertTrue(result)
        # Temp file deleted
        self.assertFalse(os.path.exists(temp_path))
        # Session gone
        self.assertIsNone(self._manager.get_session(tid))

    # ── (n) cancel download ────────────────────────────────────────────

    def test_cancel_download(self) -> None:
        self._manager = TransferSessionManager()
        filename = os.path.join(self._tmp.name, "cancel_dl.bin")
        with open(filename, "wb") as f:
            f.write(b"\x00" * 1024)
        tid, _ = self._manager.create_session(
            "download", filename, size=1024, block_size=65536, checksum="abc",
        )
        result = self._manager.cancel(tid)
        self.assertTrue(result)
        self.assertIsNone(self._manager.get_session(tid))

    # ── (o) cancel invalid id ──────────────────────────────────────────

    def test_cancel_invalid_id(self) -> None:
        self._manager = TransferSessionManager()
        result = self._manager.cancel("nonexistent")
        self.assertFalse(result)

    # ── (p) get_session invalid id ─────────────────────────────────────

    def test_get_session_invalid_id(self) -> None:
        self._manager = TransferSessionManager()
        self.assertIsNone(self._manager.get_session("nonexistent"))

    # ── (q) concurrent sessions ────────────────────────────────────────

    def test_concurrent_sessions(self) -> None:
        self._manager = TransferSessionManager()
        f1 = os.path.join(self._tmp.name, "concurrent1.bin")
        f2 = os.path.join(self._tmp.name, "concurrent2.bin")
        tid1, _ = self._manager.create_session(
            "upload", f1, size=131072, block_size=65536, checksum="abc",
        )
        tid2, _ = self._manager.create_session(
            "upload", f2, size=131072, block_size=65536, checksum="def",
        )
        self.assertNotEqual(tid1, tid2)
        # Interleave blocks
        self._manager.append_block(tid1, 0, b"AAAA")
        self._manager.append_block(tid2, 0, b"BBBB")
        self._manager.append_block(tid1, 1, b"CCCC")
        self._manager.append_block(tid2, 1, b"DDDD")
        s1 = self._manager.get_session(tid1)
        s2 = self._manager.get_session(tid2)
        assert s1 is not None
        assert s2 is not None
        self.assertEqual(s1.received_blocks, 2)
        self.assertEqual(s2.received_blocks, 2)
        self.assertEqual(os.path.getsize(s1.temp_path), 8)
        self.assertEqual(os.path.getsize(s2.temp_path), 8)


class TestTransferSessionTimeout(unittest.TestCase):
    """Tests for session timeout auto-cleanup via sweep thread."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._manager: TransferSessionManager | None = None

    def tearDown(self) -> None:
        if self._manager is not None:
            self._manager.close()

    def test_session_timeout_auto_cleanup(self) -> None:
        """Create a session with short timeout, wait, verify cleanup."""
        self._manager = TransferSessionManager(timeout_secs=0.5)
        filename = os.path.join(self._tmp.name, "timeout.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data")
        session = self._manager.get_session(tid)
        assert session is not None
        temp_path = session.temp_path
        # Wait for timeout sweep (timeout=0.5s, sweep interval=10s → need to
        # override sweep interval or just wait long enough). Since the sweep
        # interval is 10s by default, we set a very short timeout and wait
        # for the sweep. But 10s is too long for a unit test. Let's check:
        # The sweep sleeps SWEEP_INTERVAL_SECS then checks. We need a shorter
        # sweep interval. We can patch it.
        # Actually, let's just wait 11+ seconds — but that's too slow.
        # Instead, let's manually call the sweep check by waiting just past
        # the timeout and then manually triggering the sweep.
        # Wait for the timeout to expire:
        time.sleep(0.7)
        # Manually trigger sweep by calling the internal method
        self._manager._sweep_once()
        # Session should be gone
        self.assertIsNone(self._manager.get_session(tid))
        # Temp file deleted
        self.assertFalse(os.path.exists(temp_path))

    def test_session_still_alive_within_timeout(self) -> None:
        """Session is still alive while within timeout."""
        self._manager = TransferSessionManager(timeout_secs=5.0)
        filename = os.path.join(self._tmp.name, "alive.bin")
        tid, _ = self._manager.create_session(
            "upload", filename, size=1048576, block_size=65536, checksum="abc",
        )
        self._manager.append_block(tid, 0, b"data")
        time.sleep(0.3)
        # Session still alive
        self.assertIsNotNone(self._manager.get_session(tid))


if __name__ == "__main__":
    unittest.main()