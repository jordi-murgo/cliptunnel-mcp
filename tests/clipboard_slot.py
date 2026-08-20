"""Deterministic last-writer-wins clipboard test double."""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class ClipboardSlot:
    """A single shared value with explicit revisions and observable writes."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._value = ""
        self._revision = 0
        self._writes: list[str] = []

    @property
    def revision(self) -> int:
        with self._condition:
            return self._revision

    def read(self) -> str:
        with self._condition:
            return self._value

    def write(self, value: str) -> None:
        with self._condition:
            self._value = value
            self._revision += 1
            self._writes.append(value)
            self._condition.notify_all()

    def overwrite(self, value: str) -> None:
        """Schedule an external writer; the previous value is irretrievably lost."""
        self.write(value)

    def wait_for_revision(self, after: int, timeout: float = 1.0) -> int:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._revision <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._revision
                self._condition.wait(remaining)
            return self._revision

    def wait_for_write(
        self,
        predicate: Callable[[str], bool],
        *,
        after: int = 0,
        timeout: float = 1.0,
    ) -> tuple[int, str]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for index in range(after, len(self._writes)):
                    value = self._writes[index]
                    if predicate(value):
                        return index + 1, value
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError("expected clipboard write was not observed")
                self._condition.wait(remaining)
