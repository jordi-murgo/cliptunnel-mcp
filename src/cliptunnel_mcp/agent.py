"""Agent endpoint of the ClipTunnel CT3 protocol.

Runs on the locked-down remote machine. Watches an injected slot-compatible
:class:`~cliptunnel_mcp.transport.Transport` for commands, ACKs them
immediately, processes them in a worker pool, and writes one typed response
at a time — retransmitting it byte-identically until its exact ACK arrives.

CT3: each agent generates a random R+7hex remote ID and responds only to
messages addressed to that ID or to the broadcast address. When a
controller announces via ANNOUNCE, the agent registers the controller
and sends a registration response with sysinfo directed to that controller.
Responses and ACKs are routed to the specific controller ID, not a fixed
address.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""

import logging

import concurrent.futures
import json
import os
import queue
import random
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
    is_controller as is_controller_addr,
    pack,
    unpack,
    validate,
)
from cliptunnel_mcp.transport import Transport

from cliptunnel_mcp import __version__, config


logger = logging.getLogger("cliptunnel-agent")

# Agent heartbeat: periodically re-register with every known controller so
# registrations (and their sysinfo) survive controller restarts.
HEARTBEAT_ENV_VAR = "CLIPTUNNEL_HEARTBEAT_SECS"
DEFAULT_HEARTBEAT_SECS = 120.0
_HEARTBEAT_JITTER_SECS = 15.0


def _resolve_heartbeat_secs(explicit: float | None) -> float:
    """Resolve the heartbeat interval: explicit arg, then env var or config
    file, then default.

    Precedence: ``heartbeat_secs`` argument > ``CLIPTUNNEL_HEARTBEAT_SECS``
    env var > ``[heartbeat] interval_secs`` in the config file > default
    the heartbeat entirely.
    """
    if explicit is not None:
        return float(explicit)
    raw = (config.get_env(HEARTBEAT_ENV_VAR) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            logger.warning(
                "ignoring malformed %s=%r — using default %.0fs",
                HEARTBEAT_ENV_VAR, raw, DEFAULT_HEARTBEAT_SECS,
            )
    return DEFAULT_HEARTBEAT_SECS


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
        heartbeat_secs: float | None = None,
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
        self._response_queue: queue.Queue[tuple[int, str, bool, str]] = queue.Queue()
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="cliptunnel-agent-worker",
        )
        # Known controller IDs — the agent routes responses to the
        # specific controller that sent the command or announced itself.
        self._known_controllers: set[str] = set()
        # Transport backend name for sysinfo reporting.
        self._transport_backend = getattr(transport, "backend_name", None)
        # Transport endpoint for sysinfo reporting (sanitized, no secrets).
        self._transport_endpoint = getattr(transport, "endpoint", None)
        self._dispatcher_thread = threading.Thread(
            target=self._response_dispatcher,
            name="cliptunnel-agent-responder",
            daemon=True,
        )
        self._reader_thread = threading.Thread(
            target=self._reader, name="cliptunnel-agent-reader", daemon=True
        )
        # Heartbeat: periodic re-registration with every known controller.
        # A non-positive interval disables the heartbeat entirely.
        self.heartbeat_secs = _resolve_heartbeat_secs(heartbeat_secs)
        self._heartbeat_thread: threading.Thread | None = None
        if self.heartbeat_secs > 0:
            self._heartbeat_thread = threading.Thread(
                target=self._heartbeat_loop,
                name="cliptunnel-agent-heartbeat",
                daemon=True,
            )
        self._dispatcher_thread.start()
        self._reader_thread.start()
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.start()

    # ── Public API ───────────────────────────────────────────────────

    def close(self) -> None:
        """Stop this agent generation. Idempotent; never strands a thread."""
        with self._slot_lock:
            if not self._running:
                return
            self._running = False
            self._response_condition.notify_all()
        self._response_queue.put((-1, "", False, ""))
        self._dispatcher_thread.join(timeout=2.0)
        self._reader_thread.join(timeout=2.0)
        if self._heartbeat_thread is not None:
            self._heartbeat_thread.join(timeout=2.0)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def send_registration(self, controller_id: str | None = None) -> None:
        """Send a registration response with current sysinfo to a controller.

        If *controller_id* is None, sends a single broadcast registration
        so all known controllers (and any new ones) receive it. This is
        used by the heartbeat. If a specific controller_id is given, the
        registration is directed to that controller only.
        """
        from cliptunnel_mcp.operations import dispatch
        req = {"op": "sysinfo"}
        if self._transport_backend:
            req["_transport_backend"] = self._transport_backend
        if self._transport_endpoint:
            req["_transport_endpoint"] = self._transport_endpoint
        sysinfo_result, _ = dispatch(json.dumps(req))
        if controller_id is not None:
            # Directed registration to a specific controller.
            targets = [controller_id]
        else:
            # Heartbeat: single broadcast so all controllers see it.
            targets = [BROADCAST_ADDR]
        for cid in targets:
            wire = pack(Message(
                frm=self.remote_id,
                to=cid,
                seq=0,
                mtype=MsgType.RESPONSE.value,
                payload=sysinfo_result,
            ))
            self._write_slot_safe(wire)
            logger.info("registration sent to %s (remote_id=%s)", cid, self.remote_id)

    def _schedule_registration(self, delay: float | None = None, controller_id: str | None = None) -> None:
        """Schedule a registration response after a random delay."""
        d = delay if delay is not None else random.uniform(0.1, 4.0)
        logger.info("scheduling registration in %.1fs (remote_id=%s)", d, self.remote_id)
        def _delayed() -> None:
            time.sleep(d)
            if self._running:
                self.send_registration(controller_id=controller_id)
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


    def _sleep_while_running(self, seconds: float) -> bool:
        """Bounded stoppable sleep; False when the agent stopped mid-wait."""
        deadline = time.monotonic() + seconds
        while self._running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(remaining, 0.5))
        return False

    def _heartbeat_loop(self) -> None:
        """Heartbeat thread: periodically re-register with known controllers.

        Each cycle waits ``heartbeat_secs`` plus random jitter (0-15s) so
        multiple agents sharing a clipboard never synchronize their writes,
        then sends a registration (sysinfo RESPONSE seq=0) to every known
        controller through the regular registration path. Cycles are
        skipped silently while no controller is known.
        """
        while self._running:
            delay = self.heartbeat_secs + random.uniform(0.0, _HEARTBEAT_JITTER_SECS)
            if not self._sleep_while_running(delay):
                break
            if not self._running:
                break
            if self._known_controllers:
                if not self._running:
                    break
                logger.debug(
                    "heartbeat registration (remote_id=%s)", self.remote_id
                )
                try:
                    self.send_registration()
                except Exception:
                    logger.warning(
                        "heartbeat registration failed — retrying next cycle",
                        exc_info=True,
                    )


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

        # Process ANNOUNCE from a controller — register and respond with sysinfo.
        if msg.mtype == MsgType.ANNOUNCE.value:
            if is_controller_addr(msg.frm):
                logger.info("ANNOUNCE from controller %s — registering", msg.frm)
                self._known_controllers.add(msg.frm)
                self._last_raw = raw
                self._schedule_registration()
            else:
                self._last_raw = raw
            return

        # Respond to ping immediately with an ACK — no processing.
        if msg.mtype == MsgType.PING.value:
            logger.info("ping recv seq=%d — sending ACK", msg.seq)
            self._write_slot_safe(pack(Message(
                frm=self.remote_id,
                to=msg.frm,
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

        # Broadcast register (legacy compat): schedule a delayed registration.
        if msg.to == BROADCAST_ADDR:
            try:
                req = json.loads(msg.payload) if msg.payload else {}
            except Exception:
                req = {}
            if isinstance(req, dict) and req.get("op") == "register":
                logger.info("broadcast register received — scheduling registration")
                # If the sender is a controller, register it.
                if is_controller_addr(msg.frm):
                    self._known_controllers.add(msg.frm)
                self._last_raw = raw
                self._schedule_registration(controller_id=msg.frm if is_controller_addr(msg.frm) else None)
                return
            # Other broadcast commands: fall through to normal ACK + process.

        # Register the controller that sent this command.
        if is_controller_addr(msg.frm):
            self._known_controllers.add(msg.frm)

        # ACK immediately — frees the slot for the Controller.
        logger.info("recv cmd seq=%d payload=%s", msg.seq, _truncate(msg.payload))
        logger.debug("acking seq=%d", msg.seq)
        self._write_slot_safe(pack(Message(
            frm=self.remote_id,
            to=msg.frm,
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
                        self._response_queue.put((msg.seq, payload, is_error, msg.frm))
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
        self._response_queue.put((msg.seq, output, is_error, msg.frm))

    def _response_dispatcher(self) -> None:
        """Send one response envelope until its exact ACK arrives."""
        while self._running:
            try:
                seq, payload, is_error, target = self._response_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if not self._running or seq == -1:
                break

            logger.debug("send %s seq=%d", "ERR" if is_error else "RSP", seq)

            mtype = MsgType.ERROR.value if is_error else MsgType.RESPONSE.value
            wire = pack(Message(
                frm=self.remote_id,
                to=target,
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
        """Write to the slot and update _last_raw atomically.

        Catches clipboard write failures so a transient clipboard lock
        (Citrix, EDR) does not crash the responder thread.  The caller's
        retransmission loop will retry on the next tick.
        """
        try:
            with self._slot_lock:
                self._transport.write(wire)
                self._last_raw = wire
        except Exception:
            logger.warning("clipboard write failed — will retry", exc_info=True)
            time.sleep(0.5)

# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Run the Agent on the local OS clipboard (``cliptunnel-agent`` entry point).

    Builds a :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport`
    backed by the system clipboard and wires :func:`cliptunnel_mcp.operations.dispatch`
    as the command handler, then blocks until interrupted.

    Use ``--verbose`` or ``-v`` to enable DEBUG-level logging (ACKs, retransmits).
    Use ``--config PATH`` to point at a non-default TOML config file
    (default: ``~/.cliptunnel/config.toml``, also overridable via the
    ``CLIPTUNNEL_CONFIG`` environment variable).
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
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help=(
            "path to the TOML config file "
            f"(default: {config.DEFAULT_CONFIG_PATH}, "
            "overridable via CLIPTUNNEL_CONFIG)"
        ),
    )
    args = parser.parse_args()

    # Apply the --config override before anything resolves settings.
    config.set_config_path(args.config)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger("cliptunnel-agent")

    from cliptunnel_mcp.transport_factory import build_transport
    from cliptunnel_mcp.operations import dispatch as handler
    transport = build_transport()
    logger.info("starting agent on %s transport (cliptunnel-mcp %s)", transport.backend_name, __version__)
    agent = Agent(transport, handler)
    logger.info("agent running — remote_id=%s — press Ctrl+C to stop", agent.remote_id)

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
