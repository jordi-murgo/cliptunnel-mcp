"""
ClipTunnel Protocol v3 — shared protocol module.

Wire format: CT3|<from>|<to>|<seq>|<type>|<payload>

  CT3      protocol signature + version
  from     C+7-hex (Controller) or R+7-hex (Remote)
  to       C+7-hex, R+7-hex, or * (broadcast)
  seq      positive integer
  type     C (command), R (response), E (error), A (ack), P (ping), N (announce)
  payload  base64-encoded UTF-8

CT3 introduces prefixed addresses (C/R + 7 hex) to support multiple
controllers on a shared clipboard.  Controllers announce their presence
with the ANNOUNCE (N) message type; remotes respond with sysinfo directed
to the announcing controller.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import base64
import re
import secrets
from dataclasses import dataclass
from enum import Enum

PROTOCOL_SIG = "CT3"
PROTOCOL_VERSION = 3

# Broadcast address
BROADCAST_ADDR = "*"

# CT3 address patterns: C + 7 hex (controller), R + 7 hex (remote)
_CONTROLLER_ID_RE = re.compile(r"^[Cc][0-9a-f]{7}$")
_REMOTE_ID_RE = re.compile(r"^[Rr][0-9a-f]{7}$")
# Union: either prefix + 7 hex
_ENDPOINT_ID_RE = re.compile(r"^[CRcr][0-9a-f]{7}$")

# Legacy CT2 compatibility: bare 8-char hex (no prefix)
_LEGACY_HEX_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# Legacy CT2 controller address — kept for backward compatibility
CONTROLLER_ADDR = "C"


class MsgType(str, Enum):
    COMMAND = "C"
    RESPONSE = "R"
    ERROR = "E"
    ACK = "A"
    PING = "P"
    ANNOUNCE = "N"


_VALID_TYPES = frozenset(t.value for t in MsgType)


@dataclass
class Message:
    """A decoded clipboard-tunnel message."""

    frm: str       # C+7hex or R+7hex
    to: str        # C+7hex, R+7hex, or '*'
    seq: int
    mtype: str     # 'C', 'R', 'E', 'A', 'P', 'N'
    payload: str   # raw decoded UTF-8 string


def generate_controller_id() -> str:
    """Generate a random controller ID: 'C' + 7 hex chars."""
    return "C" + secrets.token_hex(4)[:7]


def generate_remote_id() -> str:
    """Generate a random remote ID: 'R' + 7 hex chars."""
    return "R" + secrets.token_hex(4)[:7]


def is_valid_from_address(addr: str) -> bool:
    """True if *addr* is a valid CT3 from-address: C+7hex, R+7hex, or legacy 8-hex.

    The broadcast address '*' is never a valid sender.
    Legacy bare 8-hex IDs (CT2) are accepted for backward compatibility.
    """
    if addr == CONTROLLER_ADDR:
        return True
    return bool(_ENDPOINT_ID_RE.match(addr) or _LEGACY_HEX_ID_RE.match(addr))


def is_valid_to_address(addr: str) -> bool:
    """True if *addr* is a valid CT3 to-address: C+7hex, R+7hex, '*', or legacy 8-hex."""
    if addr == CONTROLLER_ADDR or addr == BROADCAST_ADDR:
        return True
    return bool(_ENDPOINT_ID_RE.match(addr) or _LEGACY_HEX_ID_RE.match(addr))


def is_valid_address(addr: str) -> bool:
    """True if *addr* is a valid CT3 address (from or to).

    Kept for backward compatibility — prefer is_valid_to_address for
    the *to* field and is_valid_from_address for the *from* field.
    """
    return is_valid_to_address(addr)


def is_controller(addr: str) -> bool:
    """True if *addr* is a controller address (C+7hex or legacy 'C')."""
    if addr == CONTROLLER_ADDR:
        return True
    return bool(_CONTROLLER_ID_RE.match(addr))


def is_remote(addr: str) -> bool:
    """True if *addr* is a remote address (R+7hex)."""
    return bool(_REMOTE_ID_RE.match(addr))


def is_broadcast(addr: str) -> bool:
    """True if *addr* is the broadcast address '*'."""
    return addr == BROADCAST_ADDR


def pack(msg: Message) -> str:
    """Serialize *msg* into the wire format ``CT3|from|to|seq|type|payload``.

    The payload is base64-encoded over UTF-8 bytes so that newlines, pipe
    characters, and arbitrary unicode survive the clipboard round-trip.
    """
    encoded = base64.b64encode(msg.payload.encode("utf-8")).decode("ascii")
    return f"{PROTOCOL_SIG}|{msg.frm}|{msg.to}|{msg.seq}|{msg.mtype}|{encoded}"


def unpack(raw: str) -> Message | None:
    """Parse a wire-format string back into a :class:`Message`.

    Returns ``None`` when the input is malformed, has the wrong number of
    fields, an unknown signature, an invalid address, a non-integer seq,
    an unknown message type, or invalid base64.
    """
    if not raw:
        return None

    parts = raw.split("|")
    if len(parts) != 6:
        return None
    sig, frm, to, seq_str, mtype, encoded = parts
    if sig != PROTOCOL_SIG:
        return None
    if not is_valid_from_address(frm):
        return None
    if not is_valid_to_address(to):
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

    *my_id* is a C+7hex or R+7hex endpoint ID. Rules:
      - must start with ``CT3|``
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
        """Return the highest seq ever tracked, or None if empty."""
        return max(self._states.keys()) if self._states else None