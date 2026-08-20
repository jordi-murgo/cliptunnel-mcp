"""Controller endpoint (role C) of the ClipTunnel CT1 protocol.

Runs on the operator's machine. Sends commands to the Agent through an
injected slot-compatible :class:`~cliptunnel_mcp.transport.Transport` with
full-duplex async behavior: ``send_command`` returns a ``Future``
immediately while background threads dispatch commands serially and read
responses.

Ported from the hardened vulcano-helper Host (seq-bound ARQ, generation-safe
semantics) with the role alphabet narrowed to {C, A} and the clipboard
replaced by constructor injection — this module never imports clipboard code.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import concurrent.futures
import json
import queue
import threading
import time
from pathlib import Path
from typing import Protocol

from cliptunnel_mcp.protocol import Message, MsgType, Role, pack, unpack, validate
from cliptunnel_mcp.transport import Transport

_DEFAULT_SEQ_FILE = ".cliptunnel_controller_seq"


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

        self._send_queue: queue.Queue[tuple[int, str]] = queue.Queue()
        self._futures: dict[int, concurrent.futures.Future] = {}
        self._futures_lock = threading.Lock()
        self._slot_lock = threading.RLock()
        self._slot_condition = threading.Condition(self._slot_lock)
        self._pending_command_seq: int | None = None
        self._last_write_time = 0.0
        self._running = True

        self._dispatcher_thread = threading.Thread(
            target=self._dispatcher, name="cliptunnel-controller-dispatcher", daemon=True
        )
        self._reader_thread = threading.Thread(
            target=self._reader, name="cliptunnel-controller-reader", daemon=True
        )
        self._dispatcher_thread.start()
        self._reader_thread.start()

    # ── Public API ───────────────────────────────────────────────────

    def send_command(self, command: str) -> concurrent.futures.Future:
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
        self._send_queue.put((seq, command))
        return future

    def send_command_sync(self, command: str) -> str | None:
        """Send *command* and block until response or *timeout* seconds."""
        future = self.send_command(command)
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
        self._send_queue.put((-1, ""))
        self._dispatcher_thread.join(timeout=2.0)
        self._reader_thread.join(timeout=2.0)

    # ── Background threads ───────────────────────────────────────────

    def _dispatcher(self) -> None:
        """Serial dispatcher: write one command, wait for its exact release."""
        while self._running:
            try:
                seq, command = self._send_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._running or seq == -1:
                break

            acked = False
            for _attempt in range(self.retries):
                if not self._running:
                    break
                wire = pack(Message(
                    frm=Role.CONTROLLER.value,
                    to=Role.AGENT.value,
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
            if not validate(raw, Role.CONTROLLER):
                continue
            msg = unpack(raw)
            if msg is None:
                continue

            if msg.mtype == MsgType.ACK.value:
                with self._slot_condition:
                    if self._pending_command_seq == msg.seq:
                        self._pending_command_seq = None
                        self._slot_condition.notify_all()

            elif msg.mtype in (MsgType.RESPONSE.value, MsgType.ERROR.value):
                # Skip stale messages from a previous Controller session.
                if msg.seq <= self._min_seq:
                    continue
                # A same-seq R/E subsumes the command ACK; unrelated traffic
                # never releases the pending command.
                with self._slot_condition:
                    if self._pending_command_seq == msg.seq:
                        self._pending_command_seq = None
                        self._slot_condition.notify_all()
                # ACK the response back so the Agent stops retransmitting.
                self._paced_write(pack(Message(
                    frm=Role.CONTROLLER.value,
                    to=Role.AGENT.value,
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
