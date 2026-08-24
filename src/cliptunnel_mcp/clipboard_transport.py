"""OS-backed clipboard transport for the ClipTunnel CT3 protocol.

A real :class:`~cliptunnel_mcp.transport.Transport` backed by the system
clipboard via the `clipboard-event` package.  clipboard-event provides
cross-platform clipboard change notifications (event-driven on Windows
and Wayland, changeCount polling on macOS, hash polling on X11), plus
read/write access — this module adapts its API to the Transport and
RevisionMonitor protocols that Controller and Agent expect.

The :attr:`backend_name` property exposes the transport backend identifier
(e.g. ``"clipboard.macos"``, ``"clipboard.windows"``, ``"clipboard.wayland"``,
``"clipboard.x11"``) so it can be reported in sysinfo and used for diagnostics.

Zero external dependencies beyond clipboard-event — Python 3.10 compatible.
"""
from __future__ import annotations

import threading
import time

from clipboard_event import Clipboard

from cliptunnel_mcp.protocol import PROTOCOL_SIG


class ClipboardTransport:
    """OS clipboard backed :class:`Transport` with revision tracking.

    Satisfies both :class:`~cliptunnel_mcp.transport.Transport` and
    :class:`~cliptunnel_mcp.transport.RevisionMonitor` structurally.

    Delegates all clipboard I/O and change detection to
    :class:`clipboard_event.Clipboard`, which provides event-driven
    monitoring on Windows (WM_CLIPBOARDUPDATE) and Wayland
    (wl-paste --watch), and changeCount/hash polling on macOS and X11.
    """

    def __init__(self, *, poll_interval: float = 0.1, clipboard: Clipboard | None = None) -> None:
        self._clipboard = clipboard if clipboard is not None else Clipboard()
        self._condition = threading.Condition()
        self._value: str = self._safe_read()
        self._revision = 0
        self._running = True
        # User-clipboard preservation: the last externally-set value that is
        # not CT3 protocol traffic is retained as the user's clipboard, and
        # the guard below tracks what this process last wrote to the OS.
        self._user_backup: str | None = self._backup_candidate(self._value)
        self._last_write_value: str | None = None
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
            self._user_backup = self._backup_candidate(text) or self._user_backup
            self._value = text
            self._revision += 1
            self._condition.notify_all()

    @staticmethod
    def _backup_candidate(text: str) -> str | None:
        """User-clipboard candidate: non-empty and not CT3 protocol traffic."""
        if text and not text.startswith(PROTOCOL_SIG):
            return text
        return None

    @property
    def backend_name(self) -> str:
        """Transport backend identifier, e.g. ``'clipboard.macos'``."""
        raw = getattr(self._clipboard, "backend_name", "unknown")
        return f"clipboard.{raw}"

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
            self._last_write_value = value
            self._revision += 1
            self._condition.notify_all()

    def restore_user_clipboard(self) -> bool:
        """Write the backed-up user clipboard back, guarded, as a self-write.

        Restores only when the real OS clipboard still holds this process's
        last self-write — i.e. neither the user nor any other process has
        written since. clipboard_event does not expose a changeCount guard,
        so the actual current OS content is compared against the cached
        self-write value (never against the cached read). Returns False and
        touches nothing when there is no backup, no self-write baseline, or
        another writer intervened.
        """
        with self._condition:
            backup = self._user_backup
            baseline = self._last_write_value
        if not backup or baseline is None:
            return False
        if self._safe_read() != baseline:
            return False  # someone else wrote after our last self-write
        try:
            self.write(backup)
        except Exception:
            return False
        return True

    def force_restore_user_clipboard(self) -> bool:
        """Restore the user clipboard without checking the self-write baseline.

        Used after processing broadcast registrations where the controller
        did not write an ACK, so the clipboard still holds the agent's
        message rather than the controller's last self-write.
        """
        with self._condition:
            backup = self._user_backup
        if not backup:
            return False
        try:
            self.write(backup)
        except Exception:
            return False
        return True

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