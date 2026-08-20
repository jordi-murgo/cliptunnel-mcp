"""OS-backed clipboard transport for the ClipTunnel CT1 protocol.

A real :class:`~cliptunnel_mcp.transport.Transport` backed by the system
clipboard — the channel both endpoints share on a locked-down machine.

Platform drivers:

* **macOS** — ``pbcopy``/``pbpaste`` (polling, 100 ms).
* **Windows** — ``ctypes`` + ``user32`` (polling, 100 ms).  All Win32
  prototypes are declared with proper ``restype``/``argtypes`` so that
  64-bit handles are not truncated to ``c_int``.
* **Linux / Wayland** — ``wl-copy``/``wl-paste``.  Uses
  ``wl-paste --watch`` for *event-driven* change detection: no polling,
  zero CPU when idle, sub-millisecond latency on change.
* **Linux / X11** — ``xclip`` (fallback ``xsel``).  Polling, 100 ms;
  X11 has no usable clipboard-change notification.

The clipboard has no monotonic revision counter, so one is maintained
by the driver: every content change (detected via hash for polling
drivers, or by the watch event for Wayland) bumps ``revision`` by
exactly one and wakes blocked waiters — matching the contract that
:class:`tests.clipboard_slot.ClipboardSlot` models in-memory.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import hashlib
import os
import platform
import shutil
import subprocess
import threading
import time


# ── Platform detection ───────────────────────────────────────────────────────

def _is_wayland() -> bool:
    """True when the active session is Wayland (WAYLAND_DISPLAY set)."""
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def _have(binary: str) -> bool:
    """True when *binary* is on PATH."""
    return shutil.which(binary) is not None


# ── Windows clipboard via ctypes ─────────────────────────────────────────────

def _win_clipboard_prototypes() -> tuple[object, object]:
    """Declare Win32 prototypes so 64-bit handles are not truncated.

    Returns ``(user32, kernel32)`` with ``argtypes``/``restype`` set on
    every function we call.  Without this, ctypes defaults to ``c_int``
    return values, which silently truncates 64-bit pointers on 64-bit
    Windows and crashes the process.
    """
    import ctypes
    import ctypes.wintypes as w

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32

    user32.OpenClipboard.argtypes = [w.HWND]
    user32.OpenClipboard.restype = w.BOOL
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = w.BOOL
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = w.BOOL
    user32.GetClipboardData.argtypes = [w.UINT]
    user32.GetClipboardData.restype = w.HANDLE
    user32.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    user32.SetClipboardData.restype = w.HANDLE

    kernel32.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = w.HGLOBAL
    kernel32.GlobalLock.argtypes = [w.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [w.HGLOBAL]
    kernel32.GlobalUnlock.restype = w.BOOL
    kernel32.GlobalFree.argtypes = [w.HGLOBAL]
    kernel32.GlobalFree.restype = w.HGLOBAL

    return user32, kernel32


def _read_clipboard_bytes() -> bytes:
    """Return the current clipboard contents as raw bytes."""
    system = platform.system()
    if system == "Darwin":
        return subprocess.run(
            ["pbpaste"], capture_output=True, check=False
        ).stdout
    if system == "Windows":
        import ctypes
        import ctypes.wintypes as w

        CF_UNICODETEXT = 13
        user32, kernel32 = _win_clipboard_prototypes()

        if not user32.OpenClipboard(None):
            return b""
        try:
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return b""
            # GetClipboardData returns an HGLOBAL (global memory handle),
            # not a direct pointer.  We must GlobalLock it to get the
            # actual address, then GlobalUnlock when done.
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return b""
            try:
                str_ptr = ctypes.cast(ptr, ctypes.c_wchar_p)
                value = str_ptr.value
                if value is None:
                    return b""
                return value.encode("utf-8")
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    # Linux
    if _is_wayland() and _have("wl-paste"):
        return subprocess.run(
            ["wl-paste", "--no-newline"], capture_output=True, check=False
        ).stdout
    for cmd in (["xclip", "-selection", "clipboard", "-o"],
                ["xsel", "--clipboard", "--output"]):
        try:
            result = subprocess.run(cmd, capture_output=True, check=False)
            if result.returncode == 0:
                return result.stdout
        except FileNotFoundError:
            continue
    return b""


def _write_clipboard_bytes(data: bytes) -> None:
    """Write *data* to the system clipboard."""
    system = platform.system()
    if system == "Darwin":
        subprocess.run(["pbcopy"], input=data, check=False)
        return
    if system == "Windows":
        import ctypes
        import ctypes.wintypes as w

        CF_UNICODETEXT = 13
        GMEM_MOVEABLE = 0x0002
        user32, kernel32 = _win_clipboard_prototypes()

        text = data.decode("utf-8")
        if not user32.OpenClipboard(None):
            return
        try:
            user32.EmptyClipboard()
            if not text:
                return
            wlen = (len(text) + 1) * ctypes.sizeof(w.WCHAR)
            h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE, wlen)
            if not h_global:
                return
            ptr = kernel32.GlobalLock(h_global)
            if not ptr:
                kernel32.GlobalFree(h_global)
                return
            ctypes.memmove(ptr, ctypes.create_unicode_buffer(text), wlen)
            kernel32.GlobalUnlock(h_global)
            user32.SetClipboardData(CF_UNICODETEXT, h_global)
        finally:
            user32.CloseClipboard()
        return
    # Linux
    if _is_wayland() and _have("wl-copy"):
        subprocess.run(["wl-copy"], input=data, check=False)
        return
    for cmd in (["xclip", "-selection", "clipboard", "-i"],
                ["xsel", "--clipboard", "--input"]):
        try:
            subprocess.run(cmd, input=data, check=False)
            return
        except FileNotFoundError:
            continue


# ── Transport ────────────────────────────────────────────────────────────────

class ClipboardTransport:
    """OS clipboard backed :class:`Transport` with revision tracking.

    Satisfies both :class:`~cliptunnel_mcp.transport.Transport` and
    :class:`~cliptunnel_mcp.transport.RevisionMonitor` structurally.

    On Wayland, change detection is *event-driven* via
    ``wl-paste --watch``: a long-lived subprocess prints the clipboard
    contents on every change, and a reader thread bumps ``revision``
    immediately — no polling, no CPU when idle.

    On macOS, Windows, and X11, a daemon poller checks the clipboard
    every *poll_interval* seconds.  When the content hash changes,
    ``revision`` is bumped and waiters are woken.
    """

    def __init__(self, *, poll_interval: float = 0.1) -> None:
        self._condition: threading.Condition = threading.Condition()
        self._value: str = ""
        self._revision: int = 0
        self._poll_interval: float = poll_interval
        self._running: bool = True
        self._watch_proc: subprocess.Popen[bytes] | None = None
        self._watch_thread: threading.Thread | None = None
        self._poller_thread: threading.Thread | None = None

        # Seed with the current clipboard contents without bumping revision.
        raw = _read_clipboard_bytes()
        self._value = raw.decode("utf-8", errors="replace")

        if self._use_wayland_watch():
            self._start_wayland_watch()
        else:
            self._start_poller()

    # ── Transport interface ──────────────────────────────────────────

    def read(self) -> str:
        """Return the cached clipboard value (updated by the driver)."""
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        """Write *value* to the OS clipboard and bump revision immediately."""
        _write_clipboard_bytes(value.encode("utf-8"))
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
        """Stop the background driver (poller or wl-paste watch)."""
        with self._condition:
            self._running = False
            self._condition.notify_all()
        if self._watch_proc is not None:
            self._watch_proc.terminate()
            try:
                self._watch_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._watch_proc.kill()
        if self._watch_thread is not None:
            self._watch_thread.join(timeout=2.0)
        if self._poller_thread is not None:
            self._poller_thread.join(timeout=2.0)

    # ── Internal: driver selection ───────────────────────────────────

    @staticmethod
    def _use_wayland_watch() -> bool:
        """True when we can use ``wl-paste --watch`` (event-driven)."""
        return (
            platform.system() == "Linux"
            and _is_wayland()
            and _have("wl-paste")
        )

    # ── Internal: Wayland event-driven watch ─────────────────────────

    def _start_wayland_watch(self) -> None:
        """Start ``wl-paste --watch`` for event-driven change detection."""
        self._watch_proc = subprocess.Popen(
            ["wl-paste", "--watch", "cat"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._watch_thread = threading.Thread(
            target=self._wayland_watch_loop,
            name="cliptunnel-wayland-watcher",
            daemon=True,
        )
        self._watch_thread.start()

    def _wayland_watch_loop(self) -> None:
        """Read ``wl-paste --watch`` output and bump revision on change."""
        assert self._watch_proc is not None
        assert self._watch_proc.stdout is not None
        last_hash = hashlib.sha256(
            self._value.encode("utf-8", errors="replace")
        ).digest()
        while self._running:
            line = self._watch_proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            current_hash = hashlib.sha256(
                text.encode("utf-8", errors="replace")
            ).digest()
            if current_hash == last_hash:
                continue
            last_hash = current_hash
            with self._condition:
                self._value = text
                self._revision += 1
                self._condition.notify_all()

    # ── Internal: polling driver (macOS, Windows, X11) ───────────────

    def _start_poller(self) -> None:
        """Start a daemon poller for platforms without change events."""
        self._poller_thread = threading.Thread(
            target=self._poll_loop,
            name="cliptunnel-clipboard-poller",
            daemon=True,
        )
        self._poller_thread.start()

    def _poll_loop(self) -> None:
        last_hash = hashlib.sha256(
            self._value.encode("utf-8", errors="replace")
        ).digest()
        while self._running:
            time.sleep(self._poll_interval)
            try:
                raw = _read_clipboard_bytes()
            except Exception:
                continue
            text = raw.decode("utf-8", errors="replace")
            current_hash = hashlib.sha256(
                text.encode("utf-8", errors="replace")
            ).digest()
            if current_hash == last_hash:
                continue
            last_hash = current_hash
            with self._condition:
                self._value = text
                self._revision += 1
                self._condition.notify_all()