"""
ClipTunnel Protocol v1 — shared protocol module.

Wire format: CT1|<from>|<to>|<seq>|<type>|<payload>

  CT1      protocol signature + version
  from     C (Controller) or A (Agent)
  to       C (Controller) or A (Agent)
  seq      positive integer
  type     C (command), R (response), E (error), A (ack)
  payload  base64-encoded UTF-8

Ported from the hardened vulcano-helper CB1 H/V clipboard protocol with the
role alphabet narrowed to {C, A}; wire semantics are preserved.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from enum import Enum

PROTOCOL_SIG = "CT1"
PROTOCOL_VERSION = 1


class Role(str, Enum):
    CONTROLLER = "C"
    AGENT = "A"


class MsgType(str, Enum):
    COMMAND = "C"
    RESPONSE = "R"
    ERROR = "E"
    ACK = "A"


_VALID_ROLES = frozenset(role.value for role in Role)


@dataclass
class Message:
    """A decoded clipboard-tunnel message."""

    frm: str       # 'C' or 'A'
    to: str        # 'C' or 'A'
    seq: int
    mtype: str     # 'C', 'R', 'E', 'A'
    payload: str   # raw decoded UTF-8 string


def pack(msg: Message) -> str:
    """Serialize *msg* into the wire format ``CT1|from|to|seq|type|payload``.

    The payload is base64-encoded over UTF-8 bytes so that newlines, pipe
    characters, and arbitrary unicode survive the clipboard round-trip.
    """
    encoded = base64.b64encode(msg.payload.encode("utf-8")).decode("ascii")
    return f"{PROTOCOL_SIG}|{msg.frm}|{msg.to}|{msg.seq}|{msg.mtype}|{encoded}"


def unpack(raw: str) -> Message | None:
    """Parse a wire-format string back into a :class:`Message`.

    Returns ``None`` when the input is malformed, has the wrong number of
    fields, an unknown signature (legacy CB1 included), a role outside
    {C, A}, a non-integer seq, or invalid base64.
    """
    if not raw:
        return None

    parts = raw.split("|")
    if len(parts) != 6:
        return None
    sig, frm, to, seq_str, mtype, encoded = parts
    if sig != PROTOCOL_SIG:
        return None
    if frm not in _VALID_ROLES or to not in _VALID_ROLES:
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


def validate(raw: str, my_role: str) -> bool:
    """Return True if *raw* is a well-formed message addressed to *my_role*.

    Accepts either a :class:`Role` enum or the raw one-char string ('C'/'A').
    Rules:
      - must start with ``CT1|``
      - ``to`` must equal my role
      - ``from`` must NOT equal my role (no self-addressed messages)
      - format must be valid (parseable by :func:`unpack`)
    """
    role = my_role.value if isinstance(my_role, Role) else my_role
    if not raw or not raw.startswith(PROTOCOL_SIG + "|"):
        return False
    msg = unpack(raw)
    if msg is None:
        return False
    if msg.to != role:
        return False
    if msg.frm == role:
        return False
    return True


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
