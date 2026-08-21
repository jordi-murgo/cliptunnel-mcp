"""Controller endpoint (role C) of the ClipTunnel CT2 protocol.

Runs on the operator's machine. Sends commands to the Agent through an
injected slot-compatible :class:`~cliptunnel_mcp.transport.Transport` with
full-duplex async behavior: ``send_command`` returns a ``Future``
immediately while background threads dispatch commands serially and read
responses.

Ported from the hardened vulcano-helper Host (seq-bound ARQ, generation-safe
semantics) with the controller address 'C' and 8-hex remote IDs, and the clipboard
replaced by constructor injection — this module never imports clipboard code.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import threading
import time
from pathlib import Path
from typing import Protocol

from cliptunnel_mcp.protocol import BROADCAST_ADDR, CONTROLLER_ADDR, Message, MsgType, pack, unpack, validate
from cliptunnel_mcp.transport import Transport

_DEFAULT_SEQ_FILE = ".cliptunnel_controller_seq"

logger = logging.getLogger("cliptunnel-controller")

class SeqStore(Protocol):
    """Persistence for the Controller's last used command seq."""

    def load(self) -> int: ...

    def save(self, seq: int) -> None: ...


class FileSeqStore:
    """JSON-file-backed SeqStore; missing or corrupt files load as 0."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load(self) -> int:
        try:
            return int(json.loads(self._path.read_text("utf-8"))["seq"])
        except (OSError, ValueError, TypeError, KeyError):
            return 0

    def save(self, seq: int) -> None:
        try:
            self._path.write_text(json.dumps({"seq": seq}), "utf-8")
        except OSError:
            pass


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
        timeout: float = 120.0,
        retries: int = 3,
        poll_interval: float = 0.1,
        ack_timeout: float = 5.0,
        initial_seq: int | None = None,
        persist_seq: bool = True,
        seq_store: SeqStore | None = None,
    ) -> None:
        self._transport = transport
        # Seq persistence: an injected store wins; otherwise a file-backed
        # store under the working directory unless persistence is disabled
        # (persist_seq=False or an explicit initial_seq).
        store = seq_store
        if store is None and persist_seq and initial_seq is None:
            store = FileSeqStore(Path.cwd() / _DEFAULT_SEQ_FILE)
        self._store = store
        if initial_seq is not None:
            self.seq = initial_seq
        elif store is not None:
            self.seq = store.load()
        else:
            self.seq = 0
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
        self._registry: dict[str, dict] = {}  # remote_id -> {sysinfo + last_seen + status}
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
        # Broadcast register to discover existing remotes
        self._send_broadcast_register()

    # ── Public API ───────────────────────────────────────────────────

    def _send_broadcast_register(self) -> None:
        """Broadcast a register command to discover remotes."""
        self.seq += 1
        seq = self.seq
        if self._store is not None:
            self._store.save(seq)
        wire = pack(Message(
            frm=CONTROLLER_ADDR,
            to=BROADCAST_ADDR,
            seq=seq,
            mtype=MsgType.COMMAND.value,
            payload=json.dumps({"op": "register"}),
        ))
        with self._slot_lock:
            self._paced_write(wire)

    def _get_default_remote(self) -> str:
        """Return first alive remote ID, or broadcast if none."""
        with self._registry_lock:
            for rid, info in self._registry.items():
                if info.get("status") == "alive":
                    return rid
        return BROADCAST_ADDR

    def get_connections(self) -> dict:
        """Return a copy of the remote registry with last_seen_ago."""
        now = time.time()
        with self._registry_lock:
            result = {}
            for rid, info in self._registry.items():
                entry = dict(info)
                entry["last_seen_ago"] = round(now - info.get("last_seen", 0), 1)
                result[rid] = entry
            return result

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
                for remote_id, info in list(self._registry.items()):
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
                        # Idle too long — send a ping
                        self.seq += 1
                        ping_seq = self.seq
                        if self._store is not None:
                            self._store.save(ping_seq)
                        wire = pack(Message(
                            frm=CONTROLLER_ADDR,
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
        if self._store is not None:
            self._store.save(seq)
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
                    frm=CONTROLLER_ADDR,
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
            if not validate(raw, CONTROLLER_ADDR):
                continue
            msg = unpack(raw)
            if msg is None:
                continue

            if msg.frm != CONTROLLER_ADDR:
                with self._registry_lock:
                    if msg.frm in self._registry:
                        self._registry[msg.frm]["last_seen"] = time.time()
                        self._registry[msg.frm]["ping_sent_at"] = None
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
                    and msg.frm != CONTROLLER_ADDR
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
                if msg.mtype == MsgType.RESPONSE.value and msg.frm != CONTROLLER_ADDR:
                    try:
                        parsed = json.loads(msg.payload)
                        if isinstance(parsed, dict) and "os" in parsed:
                            with self._registry_lock:
                                self._registry[msg.frm] = {
                                    **parsed,
                                    "last_seen": time.time(),
                                    "status": "alive",
                                }
                    except (json.JSONDecodeError, TypeError):
                        pass
                # ACK the response back so the remote stops retransmitting.
                self._paced_write(pack(Message(
                    frm=CONTROLLER_ADDR,
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

    # ── Slot access ──────────────────────────────────────────────────

    def _paced_write(self, wire: str) -> None:
        """Write to the slot with a bounded inter-write gap.

        The gap (2x poll interval) gives the Agent time to read the previous
        message before it is overwritten.
        """
        with self._slot_lock:
            now = time.monotonic()
            gap = self.poll_interval * 2
            elapsed = now - self._last_write_time
            if elapsed < gap:
                time.sleep(gap - elapsed)
            self._transport.write(wire)
            self._last_write_time = time.monotonic()
