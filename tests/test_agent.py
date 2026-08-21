"""Agent endpoint behavior over a deterministic slot transport.

Adapted to the CT2 protocol: the Agent generates a random 8-hex remote_id,
uses it in the frm field, and handles PING and broadcast register commands.
"""
from __future__ import annotations

import threading
import time
import unittest

from cliptunnel_mcp import Agent
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    Message,
    MsgType,
    pack,
    unpack,
)
from tests.clipboard_slot import ClipboardSlot

WORKER_PREFIX = "cliptunnel-agent-worker"


def wire(frm: str, to: str, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm, to=to, seq=seq, mtype=kind.value, payload=payload))


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

    def deliver_command(self, agent: Agent, seq: int, payload: str = "work") -> None:
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, agent.remote_id, seq, MsgType.COMMAND, payload)
        )


class TestAgentConstruction(AgentTestCase):
    def test_constructor_generates_remote_id(self):
        agent = self.make_agent()
        self.assertEqual(len(agent.remote_id), 8)
        self.assertTrue(all(c in "0123456789abcdef" for c in agent.remote_id))

    def test_constructor_with_injected_slot_writes_nothing(self):
        self.make_agent()
        self.assertEqual(
            self.slot.wait_for_revision(after=self.slot.revision, timeout=0.02),
            self.slot.revision,
        )
        self.assertEqual(self.slot.read(), "")

    def test_each_agent_has_unique_remote_id(self):
        a1 = self.make_agent()
        a1.close()
        a2 = self.make_agent()
        self.assertNotEqual(a1.remote_id, a2.remote_id)

    def test_close_is_idempotent_and_stops_threads(self):
        agent = self.make_agent()
        agent.close()
        agent.close()
        self.assertFalse(agent._running)
        self.assertFalse(agent._dispatcher_thread.is_alive())
        self.assertFalse(agent._reader_thread.is_alive())


class TestAgentCommandHandling(AgentTestCase):
    def test_command_is_acked_immediately(self):
        agent = self.make_agent()
        self.deliver_command(agent, 1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 1))

    def test_response_written_with_handler_payload(self):
        agent = self.make_agent()
        self.deliver_command(agent, 1, "job")
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, agent.remote_id)
        self.assertEqual(message.to, CONTROLLER_ADDR)
        self.assertEqual(message.payload, "result:job")

    def test_error_result_written_as_error(self):
        agent = self.make_agent(handler=lambda payload: ("boom", True))
        self.deliver_command(agent, 1)
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )
        self.assertEqual(unpack(value).payload, "boom")

    def test_handler_exception_becomes_error_response(self):
        def handler(payload: str):
            raise RuntimeError("kaboom")

        agent = self.make_agent(handler=handler)
        self.deliver_command(agent, 1)
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

        agent = self.make_agent(handler=handler, max_workers=2)
        self.deliver_command(agent, 1, "a")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 1))
        self.deliver_command(agent, 2, "b")
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
            wire(CONTROLLER_ADDR, agent.remote_id, first_seq, MsgType.ACK)
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, second_seq),
            timeout=2.0,
        )

    def test_pool_workers_exit_after_close(self):
        agent = self.make_agent(max_workers=2)
        self.deliver_command(agent, 1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        agent.close()
        self.assertTrue(wait_until(lambda: not worker_threads(), timeout=2.0))


class TestAgentResponseCache(AgentTestCase):
    def test_duplicate_done_command_replays_cached_response(self):
        calls: list[str] = []

        def handler(payload: str):
            calls.append(payload)
            return ("result:x", False)

        agent = self.make_agent(handler=handler)
        self.deliver_command(agent, 1, "work")
        first, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )
        self.assertEqual(calls, ["work"])
        self.slot.overwrite(wire(CONTROLLER_ADDR, agent.remote_id, 1, MsgType.ACK))
        self.deliver_command(agent, 1, "work")
        _, replayed = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1), after=first
        )
        self.assertEqual(replayed, value)
        self.assertEqual(calls, ["work"])


class TestAgentGeneration(AgentTestCase):
    def test_command_after_close_is_not_acknowledged(self):
        agent = self.make_agent()
        agent.close()
        self.deliver_command(agent, 9)
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 9), timeout=0.05
            )

    def test_close_while_response_pending_stops_threads(self):
        agent = self.make_agent()
        self.deliver_command(agent, 1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        agent.close()
        self.assertFalse(agent._dispatcher_thread.is_alive())
        self.assertFalse(agent._reader_thread.is_alive())

    def test_restarted_agent_serves_new_commands_on_same_slot(self):
        first = self.make_agent()
        self.deliver_command(first, 1)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))
        first.close()
        second = self.make_agent()
        self.deliver_command(second, 1, "again")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.RESPONSE, 1))


class TestAgentPing(AgentTestCase):
    def test_ping_is_acked_immediately(self):
        """A PING message is answered with an ACK immediately — no processing,
        no response generated."""
        agent = self.make_agent()
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, agent.remote_id, 10, MsgType.PING)
        )
        _, ack = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ACK, 10)
        )
        msg = unpack(ack)
        assert msg is not None
        self.assertEqual(msg.frm, agent.remote_id)
        self.assertEqual(msg.to, CONTROLLER_ADDR)

    def test_ping_does_not_generate_response(self):
        """A PING produces only an ACK — no RESPONSE or ERROR follows."""
        agent = self.make_agent()
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, agent.remote_id, 20, MsgType.PING)
        )
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 20))
        # No response should appear within a short window
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.RESPONSE, 20), timeout=0.05
            )


class TestAgentMessageRouting(AgentTestCase):
    def test_message_addressed_to_different_remote_is_ignored(self):
        """A message with to=different_hex_id is ignored by this agent."""
        self.make_agent()
        other_id = "cafef00d"
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, other_id, 1, MsgType.COMMAND, "not for me")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 1), timeout=0.05
            )

    def test_message_addressed_to_controller_is_ignored(self):
        """A message addressed to C is ignored by the agent (it's for the
        controller, not for us)."""
        self.make_agent()
        self.slot.overwrite(
            wire("cafef00d", CONTROLLER_ADDR, 1, MsgType.RESPONSE, "not for me")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 1), timeout=0.05
            )


class TestAgentBroadcastRegister(AgentTestCase):
    def test_broadcast_register_triggers_delayed_registration(self):
        """A broadcast register command causes the Agent to schedule a delayed
        registration response (a RESPONSE with sysinfo payload) to the
        controller."""
        import json

        agent = self.make_agent()
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, BROADCAST_ADDR, 50, MsgType.COMMAND,
                 json.dumps({"op": "register"}))
        )
        # The registration response arrives after a random delay (0.1–4.0s).
        # Wait up to 5s for a RESPONSE from the agent.
        _, reg = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 0), timeout=5.0
        )
        msg = unpack(reg)
        assert msg is not None
        self.assertEqual(msg.frm, agent.remote_id)
        self.assertEqual(msg.to, CONTROLLER_ADDR)
        # The payload should be sysinfo JSON with an "os" field.
        payload = json.loads(msg.payload)
        self.assertIn("os", payload)

    def test_broadcast_register_does_not_send_ack(self):
        """A broadcast register command does NOT produce an ACK — only the
        delayed registration response."""
        agent = self.make_agent()
        import json

        self.slot.overwrite(
            wire(CONTROLLER_ADDR, BROADCAST_ADDR, 60, MsgType.COMMAND,
                 json.dumps({"op": "register"}))
        )
        # No ACK should appear within a short window
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 60), timeout=0.05
            )


if __name__ == "__main__":
    unittest.main()