"""Transfer session manager for block-based file transfer.

Manages per-transfer_id session state, temp file lifecycle, block ordering,
checksum verification, atomic rename on success, cancel cleanup, and timeout
sweep via a background daemon thread.

Used by the four file.transfer.* op handlers in operations.py via a
module-level lazy singleton.
"""
from __future__ import annotations

import hashlib
import math
import os
import platform
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field


@dataclass
class TransferSession:
    """State for a single active block transfer."""

    transfer_id: str
    direction: str  # "upload" or "download"
    filename: str  # final file path
    block_size: int
    total_blocks: int
    expected_checksum: str  # SHA-256 hex digest of complete file
    temp_path: str | None  # temp file path (upload only)
    received_blocks: int = 0  # count of blocks received (upload) or sent (download)
    last_activity: float = field(default_factory=time.time)


def _platform_safe_max_block_size() -> int:
    """Maximum block size that keeps base64 within transport slot capacity."""
    if platform.system() == "Windows":
        return 4 * 1024 * 1024  # 4 MB — Windows GlobalAlloc limit
    return 16 * 1024 * 1024  # 16 MB — generous for macOS/Linux


class TransferSessionManager:
    """Manages transfer session state, temp files, and timeout cleanup."""

    DEFAULT_TIMEOUT_SECS = 60.0
    SWEEP_INTERVAL_SECS = 10.0

    def __init__(self, timeout_secs: float | None = None) -> None:
        self._sessions: dict[str, TransferSession] = {}
        self._lock = threading.Lock()
        self._timeout_secs = timeout_secs if timeout_secs is not None else self.DEFAULT_TIMEOUT_SECS
        self._running = True
        self._sweep_thread = threading.Thread(
            target=self._sweep,
            daemon=True,
            name="transfer-session-sweep",
        )
        self._sweep_thread.start()

    def create_session(
        self,
        direction: str,
        filename: str,
        size: int,
        block_size: int,
        checksum: str,
    ) -> tuple[str, int]:
        """Allocate a new transfer session. Returns (transfer_id, total_blocks).

        For download direction, the *size* parameter is a hint — the real file
        size is computed via os.path.getsize().
        Raises ValueError on invalid direction, file not found (download),
        or block_size exceeding the platform safe maximum.
        """
        if direction not in ("upload", "download"):
            raise ValueError(
                f"direction must be 'upload' or 'download', got: {direction!r}"
            )

        safe_max = _platform_safe_max_block_size()
        if block_size > safe_max:
            raise ValueError(
                f"block_size {block_size} exceeds platform safe maximum {safe_max}"
            )

        if direction == "download":
            if not os.path.isfile(filename):
                raise ValueError(f"file not found: {filename}")
            real_size = os.path.getsize(filename)
            total_blocks = math.ceil(real_size / block_size) if block_size > 0 else 0
            temp_path: str | None = None
        else:
            real_size = size
            total_blocks = math.ceil(size / block_size) if block_size > 0 else 0
            parent = os.path.dirname(filename) or "."
            os.makedirs(parent, exist_ok=True)
            tf = tempfile.NamedTemporaryFile(
                delete=False,
                dir=parent,
                prefix=".cliptunnel_transfer_",
                suffix=".tmp",
            )
            tf.close()
            temp_path = tf.name

        transfer_id = str(uuid.uuid4())
        session = TransferSession(
            transfer_id=transfer_id,
            direction=direction,
            filename=filename,
            block_size=block_size,
            total_blocks=total_blocks,
            expected_checksum=checksum,
            temp_path=temp_path,
        )
        with self._lock:
            self._sessions[transfer_id] = session
        return (transfer_id, total_blocks)

    def get_session(self, transfer_id: str) -> TransferSession | None:
        """Return the session for *transfer_id*, or None if not found."""
        with self._lock:
            return self._sessions.get(transfer_id)

    def append_block(self, transfer_id: str, block_num: int, data: bytes) -> None:
        """Append decoded block data to the temp file. Validates ordering.

        Raises ValueError for invalid transfer_id, out-of-order, duplicate,
        or block_num exceeding total_blocks.
        """
        with self._lock:
            session = self._sessions.get(transfer_id)
            if session is None:
                raise ValueError("transfer_id invalid or expired")
            if block_num < session.received_blocks:
                raise ValueError(f"duplicate block_num {block_num}")
            if block_num >= session.total_blocks:
                raise ValueError(
                    f"block_num {block_num} exceeds total_blocks {session.total_blocks}"
                )
            if block_num > session.received_blocks:
                raise ValueError(
                    f"block_num {block_num} out of order (expected {session.received_blocks})"
                )
            # block_num == received_blocks → correct order, append
            assert session.temp_path is not None
            with open(session.temp_path, "ab") as f:
                f.write(data)
            session.received_blocks += 1
            session.last_activity = time.time()

    def read_block(self, transfer_id: str, block_num: int) -> bytes:
        """Read a block from the remote file (download direction).

        Raises ValueError for invalid transfer_id or block_num out of range.
        """
        with self._lock:
            session = self._sessions.get(transfer_id)
            if session is None:
                raise ValueError("transfer_id invalid or expired")
            if block_num >= session.total_blocks:
                raise ValueError(
                    f"block_num {block_num} exceeds total_blocks {session.total_blocks}"
                )
            offset = block_num * session.block_size
            with open(session.filename, "rb") as f:
                f.seek(offset)
                data = f.read(session.block_size)
            session.last_activity = time.time()
            return data

    def finalize_upload(self, transfer_id: str) -> tuple[str, int, bool]:
        """Verify checksum, atomic rename. Returns (path, size, verified).

        Raises ValueError if not all blocks have been received or session
        is invalid.
        On checksum match: renames temp → final, cleans up session.
        On mismatch: retains temp file, returns (temp_path, size, False).
        """
        with self._lock:
            session = self._sessions.get(transfer_id)
            if session is None:
                raise ValueError("transfer_id invalid or expired")
            if session.received_blocks != session.total_blocks:
                raise ValueError(
                    f"not all blocks received: {session.received_blocks}/{session.total_blocks}"
                )
            assert session.temp_path is not None
            # Compute SHA-256 of temp file
            sha = hashlib.sha256()
            size = 0
            with open(session.temp_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha.update(chunk)
                    size += len(chunk)
            computed = sha.hexdigest()
            if computed == session.expected_checksum:
                # Atomic rename
                os.replace(session.temp_path, session.filename)
                del self._sessions[transfer_id]
                return (session.filename, size, True)
            else:
                # Mismatch — retain temp, return temp path
                temp_path = session.temp_path
                del self._sessions[transfer_id]
                return (temp_path, size, False)

    def finalize_download(self, transfer_id: str) -> tuple[str, int]:
        """Cleanup session. Returns (path, size).

        Raises ValueError for invalid transfer_id.
        """
        with self._lock:
            session = self._sessions.get(transfer_id)
            if session is None:
                raise ValueError("transfer_id invalid or expired")
            path = session.filename
            size = os.path.getsize(path)
            del self._sessions[transfer_id]
            return (path, size)

    def cancel(self, transfer_id: str) -> bool:
        """Delete temp file (upload), invalidate session. Returns True if cancelled."""
        with self._lock:
            session = self._sessions.pop(transfer_id, None)
            if session is None:
                return False
            if session.temp_path is not None and os.path.exists(session.temp_path):
                try:
                    os.remove(session.temp_path)
                except OSError:
                    pass
            return True

    def _sweep_once(self) -> None:
        """Check all sessions for timeout and remove expired ones."""
        now = time.time()
        expired: list[str] = []
        with self._lock:
            for tid, session in self._sessions.items():
                if now - session.last_activity > self._timeout_secs:
                    expired.append(tid)
        for tid in expired:
            with self._lock:
                session = self._sessions.pop(tid, None)
            if session is not None and session.temp_path is not None:
                if os.path.exists(session.temp_path):
                    try:
                        os.remove(session.temp_path)
                    except OSError:
                        pass

    def _sweep(self) -> None:
        """Background thread: delete expired sessions."""
        while self._running:
            time.sleep(self.SWEEP_INTERVAL_SECS)
            if not self._running:
                break
            self._sweep_once()

    def close(self) -> None:
        """Stop sweep thread, cleanup all sessions."""
        self._running = False
        self._sweep_thread.join(timeout=2.0)
        with self._lock:
            tids = list(self._sessions.keys())
        for tid in tids:
            self.cancel(tid)