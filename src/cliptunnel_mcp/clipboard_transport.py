"""OS-backed clipboard transport for the ClipTunnel CT1 protocol.

A real :class:`~cliptunnel_mcp.transport.Transport` backed by the system
clipboard via the `clipboard-event` package.  clipboard-event provides
cross-platform clipboard change notifications (event-driven on Windows
and Wayland, changeCount polling on macOS, hash polling on X11), plus
read/write access — this module adapts its API to the Transport and
RevisionMonitor protocols that Controller and Agent expect.

Zero external dependencies beyond clipboard-event — Python 3.10 compatible.
"""
from __future__ import annotations

import threading
import time

from clipboard_event import Clipboard


class ClipboardTransport:
    """OS clipboard backed :class:`Transport` with revision tracking.

    Satisfies both :class:`~cliptunnel_mcp.transport.Transport` and
    :class:`~cliptunnel_mcp.transport.RevisionMonitor` structurally.

    Delegates all clipboard I/O and change detection to
    :class:`clipboard_event.Clipboard`, which provides event-driven
    monitoring on Windows (WM_CLIPBOARDUPDATE) and Wayland
    (wl-paste --watch), and changeCount/hash polling on macOS and X11.
    """

    def __init__(self, *, poll_interval: float = 0.1) -> None:
        self._clipboard = Clipboard()
        self._condition = threading.Condition()
        self._value: str = self._safe_read()
        self._revision = 0
        self._running = True
        # Start monitoring — clipboard-event handles the platform-specific
        # change detection (event-driven or polling) internally.
        self._subscription = self._clipboard.on_change(self._on_clipboard_change)

    def _safe_read(self) -> str:
        """Read clipboard, returning '' for None."""
        value = self._clipboard.read()
        return value if value is not None else ""

    def _on_clipboard_change(self, value: str | None) -> None:
        """Callback from clipboard-event when the clipboard changes externally."""
        text = value if value is not None else ""
        with self._condition:
            if text == self._value:
                return  # our own write, already bumped
            self._value = text
            self._revision += 1
            self._condition.notify_all()

    # ── Transport interface ──────────────────────────────────────────

    def read(self) -> str:
        """Return the cached clipboard value (updated by the change callback)."""
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        """Write *value* to the OS clipboard and bump revision immediately."""
        self._clipboard.write(value)
        with self._condition:
            self._value = value
            self._revision += 1
            self._condition.notify_all()

    # ── RevisionMonitor interface ────────────────────────────────────

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int:
        """Block until revision moves past *after* or *timeout* elapses."""
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._revision <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._revision
                self._condition.wait(remaining)
            return self._revision

    # ── Lifecycle ────────────────────────────────────────────────────

    def close(self) -> None:
        """Stop monitoring and release clipboard-event resources."""
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if self._subscription is not None:
            self._subscription.cancel()
            self._subscription = None
        self._clipboard.close()