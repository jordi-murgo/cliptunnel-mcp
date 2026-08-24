"""Controller endpoint of the ClipTunnel CT3 protocol.

Runs on the operator's machine. Sends commands to the Agent through an
injected slot-compatible :class:`~cliptunnel_mcp.transport.Transport` with
full-duplex async behavior: ``send_command`` returns a ``Future``
immediately while background threads dispatch commands serially and read
responses.

CT3 introduces multi-controller support: each Controller generates a unique
C+7hex ID and announces its presence via the ANNOUNCE message type. Other
controllers on the shared clipboard are tracked but not spoken to directly.

Ported from the hardened vulcano-helper Host (seq-bound ARQ, generation-safe
semantics) with prefixed controller/remote IDs and the clipboard replaced by
constructor injection — this module never imports clipboard code.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import platform
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Protocol
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    Message,
    MsgType,
    generate_controller_id,
    is_controller as is_controller_addr,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.transport import Transport


logger = logging.getLogger("cliptunnel-controller")

def _get_pkg_version() -> str:
    """Get cliptunnel-mcp version, or 'unknown' if not installed."""
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("cliptunnel-mcp")
    except Exception:
        try:
            from cliptunnel_mcp import __version__
            return __version__
        except Exception:
            return "unknown"

def _extract_pages(output: str, label: str) -> int:
    """Extract page count from a vm_stat line like 'Pages free: 1234.'."""
    import re
    m = re.search(rf"{re.escape(label)}:\s+(\d+)", output)
    return int(m.group(1)) if m else 0


def _windows_mem_total() -> int:
    """Total physical memory on Windows via GlobalMemoryStatusEx."""
    import ctypes
    buf = (ctypes.c_ubyte * 64)()
    ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0] = 64
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if kernel32.GlobalMemoryStatusEx(buf):
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulonglong))
        return ptr[1]  # offset 8
    return 0


def _windows_mem_available() -> int:
    """Available physical memory on Windows via GlobalMemoryStatusEx."""
    import ctypes
    buf = (ctypes.c_ubyte * 64)()
    ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0] = 64
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    if kernel32.GlobalMemoryStatusEx(buf):
        ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulonglong))
        return ptr[2]  # offset 16
    return 0


def _detect_shell_version() -> str:
    """Detect the user's shell and its version."""
    import shutil
    import subprocess
    shell = os.environ.get("SHELL", "")
    if not shell:
        return ""
    try:
        result = subprocess.run(
            [shell, "--version"], capture_output=True, text=True, timeout=2,
        )
        return result.stdout.strip().split("\n")[0] if result.stdout else ""
    except Exception:
        return ""


def _detect_agent_auth() -> str:
    """Check if a Copilot OAuth token is available (config file or env var)."""
    try:
        from cliptunnel_mcp.config import get_copilot_token

        if get_copilot_token():
            return "authenticated"
        return "no_token"
    except Exception:
        return "unknown"


def _detect_transport_backend(transport: Transport) -> str:
    """Get the transport backend name from the transport object."""
    return getattr(transport, "backend_name", "unknown")


def _get_mem_total() -> int:
    """Total system memory in bytes."""
    try:
        if platform.system() == "Darwin":
            import subprocess
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                import re
                page_size = 4096
                m = re.search(r"page size of (\d+) bytes", result.stdout)
                if m:
                    page_size = int(m.group(1))
                free_p = _extract_pages(result.stdout, "Pages free")
                active_p = _extract_pages(result.stdout, "Pages active")
                inactive_p = _extract_pages(result.stdout, "Pages inactive")
                wired_p = _extract_pages(result.stdout, "Pages wired down")
                spec_p = _extract_pages(result.stdout, "Pages occupied by compressor")
                total = (free_p + active_p + inactive_p + wired_p + spec_p) * page_size
                return total
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) * 1024
        elif platform.system() == "Windows":
            return _windows_mem_total()
    except Exception:
        pass
    return 0


def _get_mem_available() -> int:
    """Available system memory in bytes (free + inactive pages on macOS)."""
    try:
        if platform.system() == "Darwin":
            import subprocess
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                import re
                page_size = 4096
                m = re.search(r"page size of (\d+) bytes", result.stdout)
                if m:
                    page_size = int(m.group(1))
                free_p = _extract_pages(result.stdout, "Pages free")
                inactive_p = _extract_pages(result.stdout, "Pages inactive")
                return (free_p + inactive_p) * page_size
        elif platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        elif platform.system() == "Windows":
            return _windows_mem_available()
    except Exception:
        pass
    return 0


def _get_mem_percent_used() -> float:
    """Memory percentage used (active + wired pages / total on macOS)."""
    try:
        if platform.system() == "Darwin":
            import subprocess
            result = subprocess.run(
                ["vm_stat"], capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                import re
                page_size = 4096
                m = re.search(r"page size of (\d+) bytes", result.stdout)
                if m:
                    page_size = int(m.group(1))
                free_p = _extract_pages(result.stdout, "Pages free")
                active_p = _extract_pages(result.stdout, "Pages active")
                inactive_p = _extract_pages(result.stdout, "Pages inactive")
                wired_p = _extract_pages(result.stdout, "Pages wired down")
                spec_p = _extract_pages(result.stdout, "Pages occupied by compressor")
                total_p = free_p + active_p + inactive_p + wired_p + spec_p
                used_p = active_p + wired_p
                if total_p > 0:
                    return round(used_p * 100 / total_p, 1)
        else:
            total = _get_mem_total()
            avail = _get_mem_available()
            if total > 0:
                return round((total - avail) / total * 100, 1)
    except Exception:
        pass
    return 0.0


def _get_disk_total() -> int:
    """Total disk space for the current directory in bytes."""
    try:
        import shutil
        total, _used, _free = shutil.disk_usage(os.getcwd())
        return total
    except Exception:
        return 0


def _get_disk_free() -> int:
    """Free disk space for the current directory in bytes."""
    try:
        import shutil
        _total, _used, free = shutil.disk_usage(os.getcwd())
        return free
    except Exception:
        return 0


def _slot_revision(transport: Transport) -> int:
    """Current slot revision; 0 when the transport has no monitor half."""
    revision = getattr(transport, "revision", None)
    return revision if isinstance(revision, int) else 0


def _wait_for_slot_change(transport: Transport, after: int, timeout: float) -> int:
    """Bounded change-aware wait on the slot revision.

    Uses the monitor half when the transport exposes ``wait_for_revision``
    or ``wait_for_change``; otherwise falls back to bounded polling.
    """
    for name in ("wait_for_revision", "wait_for_change"):
        waiter = getattr(transport, name, None)
        if callable(waiter):
            return int(waiter(after, timeout))
    deadline = time.monotonic() + timeout
    while True:
        current = _slot_revision(transport)
        if current > after:
            return current
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return current
        time.sleep(min(remaining, 0.005))


class Controller:
    """Controller endpoint: async send with futures, serial dispatcher + reader.

    Commands are dispatched one at a time: the exact pending-command seq is
    published atomically with the slot write, and only an ``A(n)`` or an
    ``R/E(n)`` of that same seq releases the dispatcher (a same-seq R/E
    subsumes the ACK). ACK timeouts retransmit up to *retries* times, after
    which the future resolves to ``None``.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        controller_id: str | None = None,
        name: str | None = None,
        timeout: float = 120.0,
        retries: int = 3,
        poll_interval: float = 0.1,
        ack_timeout: float = 5.0,
        initial_seq: int | None = None,
    ) -> None:
        self._transport = transport
        self.controller_id = controller_id or generate_controller_id()
        self.name = name or self.controller_id
        self.seq = initial_seq if initial_seq is not None else 0
        # Ignore any R/E with seq <= this — stale slot content from a
        # previous Controller session.
        self._min_seq = self.seq
        self.timeout = timeout
        self.retries = retries
        self.poll_interval = poll_interval
        self.ack_timeout = ack_timeout

        self._send_queue: queue.Queue[tuple[int, str, str | None]] = queue.Queue()
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._futures_lock = threading.Lock()
        self._slot_lock = threading.RLock()
        self._slot_condition = threading.Condition(self._slot_lock)
        self._pending_command_seq: int | None = None
        self._last_write_time = 0.0
        self._remotes: dict[str, dict] = {}  # remote_id -> {sysinfo + last_seen + status}
        self._controllers: dict[str, dict] = {}  # controller_id -> {name + last_seen + status}
        self._registry_lock = threading.Lock()
        self._running = True

        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher, name="cliptunnel-controller-dispatcher", daemon=True
        )
        self._reader_thread = threading.Thread(
            target=self._reader, name="cliptunnel-controller-reader", daemon=True
        )
        self._keepalive_thread = threading.Thread(
            target=self._keepalive_loop, name="cliptunnel-controller-keepalive", daemon=True
        )
        self._dispatcher_thread.start()
        self._reader_thread.start()
        self._keepalive_thread.start()
        # Self-register so we appear in our own connections list.
        self._controllers[self.controller_id] = {
            "self": True,
            "name": self.name,
            "version": 3,
            "pid": os.getpid(),
            # ── OS ──
            "os": platform.system(),
            "os_release": platform.release(),
            "os_version": platform.version(),
            "hostname": socket.gethostname(),
            "arch": platform.machine(),
            "processor": platform.processor() or "unknown",
            # ── Python ──
            "python_version": sys.version,
            "python_executable": sys.executable,
            "python_implementation": platform.python_implementation(),
            # ── cliptunnel-mcp ──
            "cliptunnel_mcp_version": _get_pkg_version(),
            # ── User & environment ──
            "user": os.environ.get("USER") or os.environ.get("USERNAME") or "unknown",
            "cwd": os.getcwd(),
            "shell": os.environ.get("SHELL", ""),
            "shell_version": _detect_shell_version(),
            "home": os.path.expanduser("~"),
            # ── Agent auth ──
            "agent_auth": _detect_agent_auth(),
            # ── Transport backend ──
            "transport_backend": _detect_transport_backend(transport),
            "transport_endpoint": getattr(transport, "endpoint", None),
            # ── Hardware ──
            "cpu_count": os.cpu_count() or 0,
            "mem_total": _get_mem_total(),
            "mem_available": _get_mem_available(),
            "mem_percent_used": _get_mem_percent_used(),
            "disk_total": _get_disk_total(),
            "disk_free": _get_disk_free(),
            "last_seen": time.time(),
            "status": "alive",
        }
        # No announce here — the MCP server announces after identifying
        # the client. For programmatic use, call discover() manually.

    # ── Public API ───────────────────────────────────────────────────

    def _send_announce(self) -> None:
        """Broadcast an ANNOUNCE to discover remotes and other controllers."""
        self.seq += 1
        seq = self.seq
        announce_payload = {
            "role": "controller",
            "name": self.name,
            "version": 3,
        }
        # Include self-registered sysinfo so other controllers see the same data.
        with self._registry_lock:
            self_info = dict(self._controllers.get(self.controller_id, {}))
            self_info.pop("self", None)  # don't propagate self-flag
            announce_payload.update(self_info)
        wire = pack(Message(
            frm=self.controller_id,
            to=BROADCAST_ADDR,
            seq=seq,
            mtype=MsgType.ANNOUNCE.value,
            payload=json.dumps(announce_payload),
        ))
        with self._slot_lock:
            self._paced_write(wire)


    def discover(self) -> None:
        """Re-broadcast ANNOUNCE to discover remotes and other controllers.

        Useful when agents may have started after the initial announcement,
        or when the clipboard was busy and the ANNOUNCE was lost.
        """
        self._send_announce()

    def _get_default_remote(self) -> str:
        """Return first alive remote ID, or broadcast if none."""
        with self._registry_lock:
            for rid, info in self._remotes.items():
                if info.get("status") == "alive":
                    return rid
        return BROADCAST_ADDR

    def get_connections(self) -> dict:
        """Return a dict with ``controllers`` and ``remotes`` sub-dicts.

        Each entry includes ``last_seen_ago`` (seconds since last message).
        """
        now = time.time()
        with self._registry_lock:
            controllers = {}
            for cid, info in self._controllers.items():
                entry = dict(info)
                entry["last_seen_ago"] = round(now - info.get("last_seen", 0), 1)
                controllers[cid] = entry
            remotes = {}
            for rid, info in self._remotes.items():
                entry = dict(info)
                entry["last_seen_ago"] = round(now - info.get("last_seen", 0), 1)
                remotes[rid] = entry
        return {"controllers": controllers, "remotes": remotes}

    def _keepalive_loop(self) -> None:
        """Background thread: ping idle remotes, mark dead ones.

        - Ping a remote only after 5 minutes (300s) of inactivity.
        - If no response or ACK within 30s of the ping, mark as dead.
        - The 10s loop interval is just the poll cycle; actual ping timing
          is driven by last_seen, not the loop interval.
        """
        _PING_IDLE = 300.0    # ping after 5 min idle
        _DEAD_TIMEOUT = 30.0  # dead if no response 30s after ping
        _LOOP_INTERVAL = 10.0

        while self._running:
            time.sleep(_LOOP_INTERVAL)
            if not self._running:
                break
            now = time.time()
            with self._registry_lock:
                for remote_id, info in list(self._remotes.items()):
                    last_seen = info.get("last_seen", 0)
                    idle = now - last_seen
                    status = info.get("status", "alive")
                    ping_sent_at = info.get("ping_sent_at")

                    if status == "dead":
                        continue

                    if ping_sent_at is not None:
                        # We sent a ping — check if it timed out
                        if now - ping_sent_at > _DEAD_TIMEOUT:
                            info["status"] = "dead"
                            info["ping_sent_at"] = None
                            logger.info("remote %s marked dead (no response to ping in %.0fs)", remote_id, _DEAD_TIMEOUT)
                        # Still waiting for ping response — don't send another
                        continue

                    if idle > _PING_IDLE:
                        # Only ping on non-clipboard transports — clipboard
                        # uses the heartbeat and clipboard contention makes
                        # pings unreliable.
                        backend = getattr(self._transport, "backend_name", "")
                        if backend.startswith("clipboard"):
                            info["last_seen"] = now
                            continue
                        self.seq += 1
                        ping_seq = self.seq
                        wire = pack(Message(
                            frm=self.controller_id,
                            to=remote_id,
                            seq=ping_seq,
                            mtype=MsgType.PING.value,
                            payload="",
                        ))
                        with self._slot_lock:
                            self._paced_write(wire)
                        info["ping_sent_at"] = now
                        logger.info("ping sent to %s (idle %.0fs)", remote_id, idle)

    def send_command(self, command: str, remote_id: str | None = None) -> concurrent.futures.Future:
        """Send *command* asynchronously — returns a Future immediately.

        The Future resolves with the response payload (str) on success, or
        None on error or exhausted ACK retries.
        """
        self.seq += 1
        seq = self.seq
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._futures_lock:
            self._futures[seq] = future
        self._send_queue.put((seq, command, remote_id))
        return future

    def send_command_sync(self, command: str, remote_id: str | None = None) -> str | None:
        """Send *command* and block until response or *timeout* seconds."""
        future = self.send_command(command, remote_id=remote_id)
        try:
            return future.result(timeout=self.timeout)
        except concurrent.futures.TimeoutError:
            return None


    def close(self) -> None:
        """Stop background threads. Idempotent; never strands a thread."""
        with self._slot_lock:
            if not self._running:
                return
            self._running = False
            self._slot_condition.notify_all()
        self._send_queue.put((-1, "", None))
        self._dispatcher_thread.join(timeout=2.0)
        self._reader_thread.join(timeout=2.0)
        self._keepalive_thread.join(timeout=2.0)

    # ── Background threads ───────────────────────────────────────────

    def _dispatcher(self) -> None:
        """Serial dispatcher: write one command, wait for its exact release."""
        while self._running:
            try:
                seq, command, remote_id = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._running or seq == -1:
                break

            to = remote_id if remote_id else self._get_default_remote()
            acked = False
            for _attempt in range(self.retries):
                if not self._running:
                    break
                wire = pack(Message(
                    frm=self.controller_id,
                    to=to,
                    seq=seq,
                    mtype=MsgType.COMMAND.value,
                    payload=command,
                ))
                with self._slot_condition:
                    # Publish the exact pending seq atomically with the write
                    # so the reader can never observe the command first.
                    self._pending_command_seq = seq
                    self._paced_write(wire)
                acked = self._wait_for_ack(seq, self.ack_timeout)
                if acked:
                    break

            if not acked:
                with self._futures_lock:
                    future = self._futures.pop(seq, None)
                if future is not None and not future.done():
                    future.set_result(None)

    def _wait_for_ack(self, seq: int, timeout: float) -> bool:
        """Wait until A(seq) or R/E(seq) releases this command."""
        deadline = time.monotonic() + timeout
        with self._slot_condition:
            while self._running and self._pending_command_seq == seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._slot_condition.wait(remaining)
            return self._pending_command_seq != seq

    def _reader(self) -> None:
        """Reader thread: change-aware reads that resolve futures."""
        last_raw = ""
        revision = _slot_revision(self._transport)
        while self._running:
            _wait_for_slot_change(self._transport, revision, self.poll_interval)
            if not self._running:
                break
            revision = _slot_revision(self._transport)
            raw = self._transport.read()
            if not raw or raw == last_raw:
                continue
            last_raw = raw
            if not validate(raw, self.controller_id):
                continue
            msg = unpack(raw)
            if msg is None:
                continue

            # Update last_seen for any known remote or controller that writes to us.
            with self._registry_lock:
                if msg.frm in self._remotes:
                    self._remotes[msg.frm]["last_seen"] = time.time()
                    self._remotes[msg.frm]["ping_sent_at"] = None
                elif msg.frm in self._controllers:
                    self._controllers[msg.frm]["last_seen"] = time.time()

            # Process ANNOUNCE from another controller.
            if msg.mtype == MsgType.ANNOUNCE.value:
                if is_controller_addr(msg.frm) and msg.frm != self.controller_id:
                    try:
                        parsed = json.loads(msg.payload)
                        if isinstance(parsed, dict) and parsed.get("role") == "controller":
                            with self._registry_lock:
                                entry = {k: v for k, v in parsed.items() if k != "role"}
                                entry["last_seen"] = time.time()
                                entry["status"] = "alive"
                                self._controllers[msg.frm] = entry
                    except (json.JSONDecodeError, TypeError):
                        pass
                continue

            if msg.mtype == MsgType.ACK.value:
                with self._slot_condition:
                    if self._pending_command_seq == msg.seq:
                        self._pending_command_seq = None
                        self._slot_condition.notify_all()

            elif msg.mtype in (MsgType.RESPONSE.value, MsgType.ERROR.value):
                # Registration responses use seq=0 and must bypass the stale
                # filter — they are unsolicited, not replies to a command.
                is_registration = (
                    msg.mtype == MsgType.RESPONSE.value
                    and msg.seq == 0
                )
                if not is_registration:
                    # Skip stale messages from a previous Controller session.
                    if msg.seq <= self._min_seq:
                        continue
                # A same-seq R/E subsumes the command ACK; unrelated traffic
                # never releases the pending command.
                with self._slot_condition:
                    if self._pending_command_seq == msg.seq:
                        self._pending_command_seq = None
                        self._slot_condition.notify_all()
                # Check for registration response (payload with "os" field).
                if msg.mtype == MsgType.RESPONSE.value:
                    try:
                        parsed = json.loads(msg.payload)
                        if isinstance(parsed, dict) and "os" in parsed:
                            with self._registry_lock:
                                self._remotes[msg.frm] = {
                                    **parsed,
                                    "last_seen": time.time(),
                                    "status": "alive",
                                }
                    except (json.JSONDecodeError, TypeError):
                        pass
                # Broadcast registrations (heartbeat) do not need an ACK —
                # they are fire-and-forget. Directed responses get an ACK so
                # the remote stops retransmitting.
                if msg.to != BROADCAST_ADDR:
                    # ACK the response back so the remote stops retransmitting.
                    self._paced_write(pack(Message(
                        frm=self.controller_id,
                        to=msg.frm,
                        seq=msg.seq,
                        mtype=MsgType.ACK.value,
                        payload="",
                    )))
                with self._futures_lock:
                    future = self._futures.pop(msg.seq, None)
                if future is not None and not future.done():
                    if msg.mtype == MsgType.ERROR.value:
                        future.set_result(None)
                    else:
                        future.set_result(msg.payload)
                # The exchange ended — hand the clipboard back to the user.
                # For broadcast registrations (no ACK sent), the clipboard
                # holds the agent's message, not our self-write, so we need
                # force_restore. For directed responses, our ACK is the last
                # self-write, so the normal guard works.
                if msg.to == BROADCAST_ADDR:
                    self._force_restore_user_clipboard()
                else:
                    self._maybe_restore_user_clipboard()

    def _maybe_restore_user_clipboard(self) -> None:
        """Restore the user's clipboard after an exchange, if still intact.

        The Controller is the last writer in every exchange; once its final
        ACK is out, the slot belongs to the user again. The transport's own
        guard declines (returns False) when another writer intervened, which
        is a silent no-op here. The restore write respects the same pacing
        gap as every other slot write.
        """
        restore = getattr(self._transport, "restore_user_clipboard", None)
        if not callable(restore):
            return
        with self._slot_lock:
            self._pace_write_gap()
            try:
                restored = restore()
            except Exception:
                logger.debug("user clipboard restore failed", exc_info=True)
                return
            self._last_write_time = time.monotonic()
        if restored:
            logger.info("user clipboard restored after exchange")

    def _force_restore_user_clipboard(self) -> None:
        """Restore the user's clipboard without checking the self-write baseline.

        Used for broadcast registrations where no ACK was sent, so the
        clipboard still holds the agent's message.
        """
        restore = getattr(self._transport, "force_restore_user_clipboard", None)
        if not callable(restore):
            return
        with self._slot_lock:
            self._pace_write_gap()
            try:
                restored = restore()
            except Exception:
                logger.debug("user clipboard force-restore failed", exc_info=True)
                return
            self._last_write_time = time.monotonic()
        if restored:
            logger.info("user clipboard force-restored after broadcast registration")

    # ── Slot access ──────────────────────────────────────────────────

    def _paced_write(self, wire: str) -> None:
        """Write to the slot with a bounded inter-write gap.

        The gap (2x poll interval) gives the Agent time to read the previous
        message before it is overwritten.
        """
        with self._slot_lock:
            self._pace_write_gap()
            self._transport.write(wire)
            self._last_write_time = time.monotonic()

    def _pace_write_gap(self) -> None:
        """Sleep out the bounded inter-write gap (caller holds the slot lock)."""
        now = time.monotonic()
        gap = self.poll_interval * 2
        elapsed = now - self._last_write_time
        if elapsed < gap:
            time.sleep(gap - elapsed)
