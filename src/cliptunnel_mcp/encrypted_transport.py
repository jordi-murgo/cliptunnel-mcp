"""Optional AES-256-GCM encryption decorator for any transport.

Wraps any :class:`~cliptunnel_mcp.transport.Transport` + :class:`~cliptunnel_mcp.transport.RevisionMonitor`
so that values are encrypted on write and decrypted on read. The inner
transport carries opaque base64 blobs; the encryption is transparent to
the Controller and Agent.

When ``CLIPTUNNEL_AES_KEY`` is set, :func:`~cliptunnel_mcp.transport_factory.build_transport`
composes this decorator around the selected transport.
"""

from __future__ import annotations

from cliptunnel_mcp import crypto
from cliptunnel_mcp.transport import Transport

__all__ = ["EncryptedTransport"]


class EncryptedTransport:
    """Transport decorator that encrypts/decrypts with AES-256-GCM.

    Implements :class:`Transport` (read/write) and :class:`RevisionMonitor`
    (revision/wait_for_change) by delegating to the wrapped inner transport,
    encrypting on write and decrypting on read.

    :param inner: the underlying transport (ClipboardTransport, HttpsTransport, etc.)
    :param aes_key: 32-byte AES-256 key.
    """

    def __init__(self, inner: Transport, aes_key: bytes) -> None:
        self._inner = inner
        self._aes_key = aes_key

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        inner_name = getattr(self._inner, "backend_name", "unknown")
        return f"encrypted:{inner_name}"

    @property
    def endpoint(self) -> str | None:
        """Sanitized transport endpoint from the inner transport."""
        return getattr(self._inner, "endpoint", None)

    def read(self) -> str:
        """Read and decrypt the current value from the inner transport."""
        raw = self._inner.read()
        if not raw:
            return raw
        # Strip the CT3P prefix added on write.
        if raw.startswith("CT3P|"):
            raw = raw[5:]
        try:
            return crypto.decrypt(raw, self._aes_key)
        except (ValueError, Exception) as exc:
            # If decryption fails, return the raw value (might be plaintext
            # from a pre-encryption era, or a corrupted blob).
            # Log silently — the caller will see raw data.
            return raw

    def write(self, value: str) -> None:
        """Encrypt and write the value to the inner transport."""
        blob = crypto.encrypt(value, self._aes_key)
        # Prefix with CT3P so the clipboard transport recognizes this as
        # protocol traffic and does not back it up as user clipboard content.
        self._inner.write(f"CT3P|{blob}")

    def force_restore_user_clipboard(self) -> bool:
        """Delegate clipboard restore to the inner transport."""
        restore = getattr(self._inner, "force_restore_user_clipboard", None)
        if callable(restore):
            return restore()
        return False

    # ------------------------------------------------------------------
    # RevisionMonitor protocol (delegate to inner)
    # ------------------------------------------------------------------

    @property
    def revision(self) -> int:
        return getattr(self._inner, "revision", 0)

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
        """Block until the inner transport's revision exceeds *after*."""
        waiter = getattr(self._inner, "wait_for_change", None)
        if waiter is not None:
            return waiter(after, timeout)
        # Fallback: poll revision.
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.revision > after:
                return self.revision
            time.sleep(0.01)
        return self.revision

    # ------------------------------------------------------------------
    # Lifecycle / passthrough
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the inner transport. Idempotent."""
        close = getattr(self._inner, "close", None)
        if close is not None:
            close()

    def restore_user_clipboard(self) -> bool:
        """Delegate restore to the inner transport if it supports it."""
        restore = getattr(self._inner, "restore_user_clipboard", None)
        if restore is not None:
            return restore()
        return False