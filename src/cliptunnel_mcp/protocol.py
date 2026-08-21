"""
ClipTunnel Protocol v2 — shared protocol module.

Wire format: CT2|<from>|<to>|<seq>|<type>|<payload>

  CT2      protocol signature + version
  from     C (Controller) or 8-char hex (remote ID)
  to       C (Controller), * (broadcast), or 8-char hex (specific remote)
  seq      positive integer
  type     C (command), R (response), E (error), A (ack), P (ping)
  payload  base64-encoded UTF-8

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from enum import Enum

PROTOCOL_SIG = "CT2"
PROTOCOL_VERSION = 2

# Controller address
CONTROLLER_ADDR = "C"
# Broadcast address
BROADCAST_ADDR = "*"

# 8-char hex ID pattern
_HEX_ID_RE = re.compile(r"^[0-9a-f]{8}$")


class MsgType(str, Enum):
    COMMAND = "C"
    RESPONSE = "R"
    ERROR = "E"
    ACK = "A"
    PING = "P"


_VALID_TYPES = frozenset(t.value for t in MsgType)


@dataclass
class Message:
    """A decoded clipboard-tunnel message."""

    frm: str       # 'C' or 8-char hex
    to: str        # 'C', '*', or 8-char hex
    seq: int
    mtype: str     # 'C', 'R', 'E', 'A', 'P'
    payload: str   # raw decoded UTF-8 string


def generate_remote_id() -> str:
    """Generate a random 8-char hex remote ID."""
    return secrets.token_hex(4)


def is_valid_address(addr: str) -> bool:
    """True if *addr* is a valid CT2 address: 'C', '*', or 8-char hex."""
    if addr == CONTROLLER_ADDR or addr == BROADCAST_ADDR:
        return True
    return bool(_HEX_ID_RE.match(addr))


def is_controller(addr: str) -> bool:
    """True if *addr* is the controller address 'C'."""
    return addr == CONTROLLER_ADDR


def is_broadcast(addr: str) -> bool:
    """True if *addr* is the broadcast address '*'."""
    return addr == BROADCAST_ADDR


def pack(msg: Message) -> str:
    """Serialize *msg* into the wire format ``CT2|from|to|seq|type|payload``.

    The payload is base64-encoded over UTF-8 bytes so that newlines, pipe
    characters, and arbitrary unicode survive the clipboard round-trip.
    """
    encoded = base64.b64encode(msg.payload.encode("utf-8")).decode("ascii")
    return f"{PROTOCOL_SIG}|{msg.frm}|{msg.to}|{msg.seq}|{msg.mtype}|{encoded}"


def unpack(raw: str) -> Message | None:
    """Parse a wire-format string back into a :class:`Message`.

    Returns ``None`` when the input is malformed, has the wrong number of
    fields, an unknown signature, an invalid address (not 'C', '*', or
    8-char hex), a non-integer seq, an unknown message type, or invalid
    base64.
    """
    if not raw:
        return None

    parts = raw.split("|")
    if len(parts) != 6:
        return None
    sig, frm, to, seq_str, mtype, encoded = parts
    if sig != PROTOCOL_SIG:
        return None
    if not is_valid_address(frm):
        return None
    if not is_valid_address(to):
        return None
    if mtype not in _VALID_TYPES:
        return None
    try:
        seq = int(seq_str)
    except ValueError:
        return None
    try:
        payload = base64.b64decode(encoded, validate=True).decode("utf-8")
    except Exception:
        return None
    return Message(frm=frm, to=to, seq=seq, mtype=mtype, payload=payload)


def validate(raw: str, my_id: str) -> bool:
    """Return True if *raw* is a well-formed message addressed to *my_id*.

    *my_id* is ``'C'`` for the controller or an 8-char hex string for a
    remote. Rules:
      - must start with ``CT2|``
      - ``to`` must equal *my_id* or ``'*'`` (broadcast)
      - ``from`` must NOT equal *my_id* (no self-addressed messages)
      - format must be valid (parseable by :func:`unpack`)
    """
    if not raw or not raw.startswith(PROTOCOL_SIG + "|"):
        return False
    msg = unpack(raw)
    if msg is None:
        return False
    if msg.frm == my_id:
        return False
    if msg.to == my_id or msg.to == BROADCAST_ADDR:
        return True
    return False


class SeqTracker:
    """Tracks per-seq state for async dedupe: new → processing → done.

    States:
      - 'new':        seq never seen; should_process() returns True
      - 'processing': seq submitted to worker; duplicates resend ACK
      - 'done':       seq finished; duplicates resend cached R/E

    Cached responses are typed: the R/E discriminator (``is_error``) is
    stored alongside the payload, so a duplicate error stays an error.
    """

    def __init__(self) -> None:
        # seq -> 'processing' | 'done'
        self._states: dict[int, str] = {}
        # seq -> (cached response payload, is_error) (set when done)
        self._responses: dict[int, tuple[str, bool]] = {}

    def should_process(self, seq: int) -> bool:
        """Return True if *seq* is new (never seen)."""
        return seq not in self._states

    def mark_processing(self, seq: int) -> None:
        """Mark *seq* as actively being processed."""
        self._states[seq] = "processing"

    def mark_done(self, seq: int, response: str, is_error: bool = False) -> None:
        """Mark *seq* as done and cache its response identity."""
        self._states[seq] = "done"
        self._responses[seq] = (response, is_error)

    def get_state(self, seq: int) -> str:
        """Return 'new', 'processing', or 'done' for *seq*."""
        return self._states.get(seq, "new")

    def get_cached(self, seq: int) -> str | None:
        """Return the cached payload for a done *seq*, or None."""
        cached = self._responses.get(seq)
        return cached[0] if cached is not None else None

    def get_cached_response(self, seq: int) -> tuple[str, bool] | None:
        """Return the cached payload and R/E discriminator."""
        return self._responses.get(seq)

    @property
    def last_seq(self) -> int | None:
        """Return the highest seq seen so far, or None when empty."""
        return max(self._states.keys()) if self._states else None