"""Deprecated thin pass-through transport decorator.

Encryption is now handled at the protocol level (:func:`~cliptunnel_mcp.protocol.pack`
and :func:`~cliptunnel_mcp.protocol.unpack`) via the ``CT3E|`` wire format.
This class is retained only for backward-compatibility with code that still
wraps a transport in it; it delegates read/write to the inner transport with
no encryption.
"""

from __future__ import annotations

from cliptunnel_mcp.transport import Transport

__all__ = ["EncryptedTransport"]


class EncryptedTransport:
    """Thin pass-through decorator — no encryption.

    All read/write/monitor calls delegate to the wrapped inner transport.
    The ``aes_key`` parameter is accepted for backward compatibility but
    has no effect.
    """

    def __init__(self, inner: Transport, aes_key: bytes | None = None) -> None:
        self._inner = inner
        self._aes_key = aes_key  # unused — kept for backward compat

    # ------------------------------------------------------------------
    # Transport protocol
    # ------------------------------------------------------------------

    @property
    def backend_name(self) -> str:
        inner_name = getattr(self._inner, "backend_name", "unknown")
        return f"passthrough:{inner_name}"

    @property
    def endpoint(self) -> str | None:
        """Sanitized transport endpoint from the inner transport."""
        return getattr(self._inner, "endpoint", None)

    def read(self) -> str:
        """Read directly from the inner transport (no decryption)."""
        return self._inner.read()

    def write(self, value: str) -> None:
        """Write directly to the inner transport (no encryption)."""
        self._inner.write(value)

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