"""Agent endpoint behavior over a deterministic slot transport.

Ported from vulcano-helper VDI semantics (tests/test_clipboard_transport.py
VDISlotContracts + vdi.py), adapted to the CT1 {C, A} role alphabet and
constructor injection: the handler, worker-pool size, and timeouts are all
injectable and no clipboard code is imported.
"""
from __future__ import annotations

import threading
import time
import unittest

from cliptunnel_mcp import Agent
from cliptunnel_mcp.protocol import Message, MsgType, Role, pack, unpack
from tests.clipboard_slot import ClipboardSlot

WORKER_PREFIX = "cliptunnel-agent-worker"


def wire(frm: Role, to: Role, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm.value, to=to.value, seq=seq, mtype=kind.value, payload=payload))


def is_message(value: str, kind: MsgType, seq: int) -> bool:
    message = unpack(value)
    return message is not None and message.mtype == kind.value and message.seq == seq


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Bounded wait for *predicate* to become true."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
    return True


def worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(WORKER_PREFIX)]


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.slot = ClipboardSlot()

    def make_agent(self, handler=None, **overrides) -> Agent:
        params: dict = {
            "poll_interval": 0.001,
            "max_workers": 1,
            "response_ack_timeout": 0.02,
        }
        params.update(overrides)
        agent = Agent(
            self.slot,
            handler or (lambda payload: (f"result:{payload}", False)),
            **params,
        )
        self.addCleanup(agent.close)
        return agent

    def deliver_command(self, seq: int, payload: str = "work") -> None:
        self.slot.overwrite(
            wire(Role.CONTROLLER, Role.AGENT, seq, MsgType.COMMAND, payload)
        )


class TestAgentConstruction(AgentTestCase):
    def test_constructor_with_injected_slot_writes_nothing(self):
        self.make_agent()
        self.assertEqual(
            self.slot.wait_for_revision(after=self.slot.revision, timeout=0.02),
            self.slot.revision,
        )
        self.assertEqual(self.slot.read(), "")

    def test_close_is_idempotent_and_stops_threads(self):
        agent = self.make_agent()
        agent.close()
        agent.close()
        self.assertFalse(agent._running)
        self.assertFalse(agent._dispatcher_thread.is_alive())
        self.assertFalse(agent._reader_thread.is_alive())


class TestAgentCommandHandling(AgentTestCase):
    def test_command_is_acked_immediately(self):
        self.make_agent()
        self.deliver_command(1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 1))

    def test_response_written_with_handler_payload(self):
        self.make_agent()
        self.deliver_command(1, "job")
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, Role.AGENT.value)
        self.assertEqual(message.to, Role.CONTROLLER.value)
        self.assertEqual(message.payload, "result:job")

    def test_error_result_written_as_error(self):
        self.make_agent(handler=lambda payload: ("boom", True))
        self.deliver_command(1)
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )
        self.assertEqual(unpack(value).payload, "boom")

    def test_handler_exception_becomes_error_response(self):
        def handler(payload: str):
            raise RuntimeError("kaboom")

        self.make_agent(handler=handler)
        self.deliver_command(1)
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )
        self.assertIn("kaboom", unpack(value).payload)


class TestAgentWorkerPool(AgentTestCase):
    def test_pool_processes_two_commands_concurrently(self):
        barrier = threading.Barrier(2, timeout=2.0)

        def handler(payload: str):
            barrier.wait()
            return (f"done:{payload}", False)

        self.make_agent(handler=handler, max_workers=2)
        self.deliver_command(1, "a")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 1))
        self.deliver_command(2, "b")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 2))
        # Both handlers block on the barrier; only a pool of >= 2 workers can
        # pass it, so observing both responses proves concurrent processing.
        # Responses serialize behind their ACKs in completion order (not
        # command order), so release whichever response lands first.
        _, first_value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
            or is_message(value, MsgType.RESPONSE, 2),
            timeout=2.0,
        )
        first_seq = unpack(first_value).seq
        second_seq = 2 if first_seq == 1 else 1
        self.slot.overwrite(
            wire(Role.CONTROLLER, Role.AGENT, first_seq, MsgType.ACK)
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, second_seq),
            timeout=2.0,
        )

    def test_pool_workers_exit_after_close(self):
        agent = self.make_agent(max_workers=2)
        self.deliver_command(1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        agent.close()
        self.assertTrue(wait_until(lambda: not worker_threads(), timeout=2.0))


class TestAgentResponseCache(AgentTestCase):
    def test_duplicate_done_command_replays_cached_response(self):
        calls: list[str] = []

        def handler(payload: str):
            calls.append(payload)
            return ("result:x", False)

        self.make_agent(handler=handler)
        self.deliver_command(1, "work")
        first, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )
        self.assertEqual(calls, ["work"])
        self.slot.overwrite(wire(Role.CONTROLLER, Role.AGENT, 1, MsgType.ACK))
        self.deliver_command(1, "work")
        _, replayed = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1), after=first
        )
        self.assertEqual(replayed, value)
        self.assertEqual(calls, ["work"])


class TestAgentGeneration(AgentTestCase):
    def test_command_after_close_is_not_acknowledged(self):
        agent = self.make_agent()
        agent.close()
        self.deliver_command(9)
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 9), timeout=0.05
            )

    def test_close_while_response_pending_stops_threads(self):
        agent = self.make_agent()
        self.deliver_command(1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        agent.close()
        self.assertFalse(agent._dispatcher_thread.is_alive())
        self.assertFalse(agent._reader_thread.is_alive())

    def test_restarted_agent_serves_new_commands_on_same_slot(self):
        first = self.make_agent()
        self.deliver_command(1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        first.close()
        second = self.make_agent()
        self.deliver_command(1, "again")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))


if __name__ == "__main__":
    unittest.main()
