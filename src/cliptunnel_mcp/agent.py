"""Agent endpoint of the ClipTunnel CT2 protocol.

Runs on the locked-down remote machine. Watches an injected slot-compatible
:class:`~cliptunnel_mcp.transport.Transport` for commands, ACKs them
immediately, processes them in a worker pool, and writes one typed response
at a time — retransmitting it byte-identically until its exact ACK arrives.

Each agent generates a random 8-hex remote ID and responds only to messages
addressed to that ID or to the broadcast address. On startup and on broadcast
register commands it sends a registration response with sysinfo.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
import logging

import concurrent.futures
import queue
import threading
import time
from collections.abc import Callable

from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    Message,
    MsgType,
    SeqTracker,
    generate_remote_id,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.transport import Transport


logger = logging.getLogger("cliptunnel-agent")

Handler = Callable[[str], tuple[str, bool]]


def _truncate(s: str, maxlen: int = 120) -> str:
    """Truncate a string for logging, showing the start and length."""
    if len(s) <= maxlen:
        return s
    return s[:maxlen] + f"... ({len(s)} bytes total)"


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


class Agent:
    """Agent endpoint: immediate ACK, worker pool, serialized typed responses.

    Exactly one response envelope is pending at a time; only the matching
    ``A(seq)`` from the Controller releases it (a new command never
    implicitly acks a pending response). Responses are retransmitted on ACK
    timeout and replayed from the typed cache for duplicate commands. All
    stop state and queues are generation-local to this instance, so closing
    and starting a new Agent never strands threads.
    """

    def __init__(
        self,
        transport: Transport,
        handler: Handler,
        *,
        poll_interval: float = 0.1,
        max_workers: int = 5,
        response_ack_timeout: float = 1.0,
    ) -> None:
        self.remote_id = generate_remote_id()
        self.tracker = SeqTracker()
        self.poll_interval = poll_interval
        self.response_ack_timeout = response_ack_timeout
        self._transport = transport
        self._handler = handler
        self._running = True
        self._last_raw = ""
        self._slot_lock = threading.RLock()
        self._response_condition = threading.Condition(self._slot_lock)
        self._pending_response: tuple[int, str] | None = None
        self._response_queue: queue.Queue[tuple[int, str, bool]] = queue.Queue()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cliptunnel-agent-worker",
        )
        self._dispatcher_thread = threading.Thread(
            target=self._response_dispatcher,
            name="cliptunnel-agent-responder",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader, name="cliptunnel-agent-reader", daemon=True
        )
        self._dispatcher_thread.start()
        self._reader_thread.start()

    # ── Public API ───────────────────────────────────────────────────

    def close(self) -> None:
        """Stop this agent generation. Idempotent; never strands a thread."""
        with self._slot_lock:
            if not self._running:
                return
            self._running = False
            self._response_condition.notify_all()
        self._response_queue.put((-1, "", False))
        self._dispatcher_thread.join(timeout=2.0)
        self._reader_thread.join(timeout=2.0)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def send_registration(self) -> None:
        """Send a registration response with current sysinfo to the controller."""
        import json
        from cliptunnel_mcp.operations import dispatch
        sysinfo_result, _ = dispatch(json.dumps({"op": "sysinfo"}))
        wire = pack(Message(
            frm=self.remote_id,
            to=CONTROLLER_ADDR,
            seq=0,
            mtype=MsgType.RESPONSE.value,
            payload=sysinfo_result,
        ))
        self._write_slot_safe(wire)

    def _schedule_registration(self, delay: float | None = None) -> None:
        """Schedule a registration response after a random delay."""
        import random
        d = delay if delay is not None else random.uniform(0.1, 4.0)
        def _delayed() -> None:
            time.sleep(d)
            if self._running:
                self.send_registration()
        threading.Thread(target=_delayed, daemon=True, name="cliptunnel-register").start()

    # ── Background threads ───────────────────────────────────────────

    def _reader(self) -> None:
        """Reader thread: change-aware reads of the shared slot."""
        revision = _slot_revision(self._transport)
        while self._running:
            _wait_for_slot_change(self._transport, revision, self.poll_interval)
            if not self._running:
                break
            revision = _slot_revision(self._transport)
            self._tick()

    def _tick(self) -> None:
        raw = self._transport.read()

        # Skip if the slot is unchanged or empty.
        if not raw:
            return
        if raw == self._last_raw:
            return

        if not validate(raw, self.remote_id):
            self._last_raw = raw
            return

        msg = unpack(raw)
        if msg is None:
            self._last_raw = raw
            return

        # Ignore messages addressed to another remote (validate already
        # checks that the message is for us or broadcast, but be explicit).
        if msg.to != self.remote_id and msg.to != BROADCAST_ADDR:
            self._last_raw = raw
            return

        # Respond to ping immediately with an ACK — no processing.
        if msg.mtype == MsgType.PING.value:
            self._write_slot_safe(pack(Message(
                frm=self.remote_id,
                to=CONTROLLER_ADDR,
                seq=msg.seq,
                mtype=MsgType.ACK.value,
                payload="",
            )))
            self._last_raw = raw
            return

        # Only the Controller's matching ACK releases the pending response.
        if msg.mtype == MsgType.ACK.value:
            with self._response_condition:
                if (
                    self._pending_response is not None
                    and self._pending_response[0] == msg.seq
                ):
                    logger.debug("ack recv seq=%d — response released", msg.seq)
                    self._pending_response = None
                    self._response_condition.notify_all()
                self._last_raw = raw
            return

        # Only commands trigger processing.
        if msg.mtype != MsgType.COMMAND.value:
            self._last_raw = raw
            return

        # Broadcast register: schedule a delayed registration response, no ACK.
        if msg.to == BROADCAST_ADDR:
            try:
                import json as _json
                req = _json.loads(msg.payload) if msg.payload else {}
            except Exception:
                req = {}
            if isinstance(req, dict) and req.get("op") == "register":
                self._last_raw = raw
                self._schedule_registration()
                return
            # Other broadcast commands: fall through to normal ACK + process.

        # ACK immediately — frees the slot for the Controller.
        logger.info("recv cmd seq=%d payload=%s", msg.seq, _truncate(msg.payload))
        logger.debug("acking seq=%d", msg.seq)
        self._write_slot_safe(pack(Message(
            frm=self.remote_id,
            to=CONTROLLER_ADDR,
            seq=msg.seq,
            mtype=MsgType.ACK.value,
            payload="",
        )))

        # Dedupe: duplicates are ACKed above; done ones replay the cached
        # response, in-flight ones are already being processed.
        if not self.tracker.should_process(msg.seq):
            if self.tracker.get_state(msg.seq) == "done":
                cached = self.tracker.get_cached_response(msg.seq)
                if cached is not None:
                    with self._response_condition:
                        already_pending = (
                            self._pending_response is not None
                            and self._pending_response[0] == msg.seq
                        )
                    if not already_pending:
                        payload, is_error = cached
                        self._response_queue.put((msg.seq, payload, is_error))
            return

        self.tracker.mark_processing(msg.seq)
        self._pool.submit(self._process_and_enqueue, msg)

    def _process_and_enqueue(self, msg: Message) -> None:
        """Pool worker: run the handler and enqueue the typed response."""
        logger.info("exec seq=%d", msg.seq)
        try:
            output, is_error = self._handler(msg.payload)
        except Exception as exc:
            logger.warning("exec seq=%d error: %s", msg.seq, _truncate(str(exc)))
            output, is_error = str(exc), True
        self.tracker.mark_done(msg.seq, output, is_error)
        logger.info(
            "done seq=%d %s output=%s",
            msg.seq, "ERROR" if is_error else "OK", _truncate(output),
        )
        self._response_queue.put((msg.seq, output, is_error))

    def _response_dispatcher(self) -> None:
        """Send one response envelope until its exact ACK arrives."""
        while self._running:
            try:
                seq, payload, is_error = self._response_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._running or seq == -1:
                break

            logger.debug("send %s seq=%d", "ERR" if is_error else "RSP", seq)

            mtype = MsgType.ERROR.value if is_error else MsgType.RESPONSE.value
            wire = pack(Message(
                frm=self.remote_id,
                to=CONTROLLER_ADDR,
                seq=seq,
                mtype=mtype,
                payload=payload,
            ))

            with self._response_condition:
                # Wait for any previous response to be acked; a new command
                # never releases it, only the exact ACK does.
                while self._pending_response is not None and self._running:
                    self._response_condition.wait()
                if not self._running:
                    break
                self._pending_response = (seq, wire)
                while self._running and self._pending_response == (seq, wire):
                    self._write_slot_safe(wire)
                    self._response_condition.wait(self.response_ack_timeout)

    # ── Slot access ──────────────────────────────────────────────────

    def _write_slot_safe(self, wire: str) -> None:
        """Write to the slot and update _last_raw atomically."""
        with self._slot_lock:
            self._transport.write(wire)
            self._last_raw = wire


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Run the Agent on the local OS clipboard (``cliptunnel-agent`` entry point).

    Builds a :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`
    backed by the system clipboard and wires :func:`cliptunnel_mcp.operations.dispatch`
    as the command handler, then blocks until interrupted.

    Use ``--verbose`` or ``-v`` to enable DEBUG-level logging (ACKs, retransmits).
    """
    import argparse
    import logging
    import signal
    import sys

    parser = argparse.ArgumentParser(description="ClipTunnel agent")
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="enable DEBUG-level logging (ACKs, retransmits, slot writes)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("cliptunnel-agent")

    from cliptunnel_mcp.clipboard_transport import ClipboardTransport
    from cliptunnel_mcp.operations import dispatch as handler

    logger.info("starting agent on local OS clipboard")
    transport = ClipboardTransport()
    logger.info("clipboard transport ready (revision=%d)", transport.revision)
    agent = Agent(transport, handler)
    logger.info("agent running — press Ctrl+C to stop")

    # Send unsolicited registration on startup.
    agent._schedule_registration()

    def _shutdown(signum: int, frame: object) -> None:
        logger.info("received signal %d, shutting down", signum)
        agent.close()
        transport.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    # SIGTERM is not deliverable on Windows; only register if available.
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _shutdown)
        except (OSError, ValueError):
            pass

    # Block forever; background threads do the work.
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        agent.close()
        transport.close()


if __name__ == "__main__":
    main()
