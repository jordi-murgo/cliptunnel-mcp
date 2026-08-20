"""Transport interfaces for the ClipTunnel clipboard channel.

The protocol layer (:mod:`cliptunnel_mcp.protocol`) is pure: it never
imports clipboard code. Endpoints (Controller/Agent) receive
:class:`Transport` and a :class:`RevisionMonitor` injected, and the
deterministic test double :class:`tests.clipboard_slot.ClipboardSlot`
models the channel both halves describe:

  * a single shared string value — the clipboard slot;
  * last-writer-wins: every write replaces the value entirely and the
    previous value is irretrievably lost;
  * a monotonic revision counter bumped by exactly one on every write;
  * blocked waiters are woken on every write (condition-variable
    semantics, not polling races).

ClipboardSlot provides ``read``/``write`` — structurally satisfying
:class:`Transport` — and models the monitor half with ``revision`` plus
``wait_for_revision(after, timeout)``; :class:`RevisionMonitor` names the
generic ``wait_for_change`` primitive that real monitors implement.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Transport(Protocol):
    """Read/write access to the shared clipboard slot.

    Implementations must be safe for concurrent use by endpoint threads.
    ``read`` returns the current value; ``write`` replaces it entirely
    (last-writer-wins — the previous value is lost) and wakes any waiters
    blocked in a monitor.
    ``tests.clipboard_slot.ClipboardSlot`` is the deterministic reference
    double used by the test suite.
    """

    def read(self) -> str: ...

    def write(self, value: str) -> None: ...


@runtime_checkable
class RevisionMonitor(Protocol):
    """Monotonic revision tracking over a last-writer-wins slot.

    Every write bumps ``revision`` by exactly one and wakes blocked
    waiters. ``wait_for_change`` blocks until the revision moves past
    ``after`` or ``timeout`` seconds elapse, then returns the current
    revision — it never raises on timeout (a bounded wait).
    ``tests.clipboard_slot.ClipboardSlot`` models this contract with
    ``revision`` and ``wait_for_revision(after, timeout)``.
    """

    @property
    def revision(self) -> int: ...

    def wait_for_change(self, after: int, timeout: float = 1.0) -> int: ...
