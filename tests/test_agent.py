"""Agent endpoint behavior over a deterministic slot transport.

Adapted to the CT3 protocol: the Agent generates a random R+7hex remote_id,
uses it in the frm field, and handles PING, ANNOUNCE from controllers, and
broadcast register commands (legacy compat).
"""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from unittest import mock


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

TEST_CONTROLLER_ID = "C1a2b3c4"


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
            wire(TEST_CONTROLLER_ID, agent.remote_id, seq, MsgType.COMMAND, payload)
        )


class TestAgentConstruction(AgentTestCase):
    def test_constructor_generates_remote_id(self):
        agent = self.make_agent()
        self.assertEqual(len(agent.remote_id), 8)
        self.assertTrue(agent.remote_id.startswith("R"))
        self.assertTrue(all(c in "0123456789abcdef" for c in agent.remote_id[1:]))

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
        self.assertEqual(message.to, TEST_CONTROLLER_ID)
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
            wire(TEST_CONTROLLER_ID, agent.remote_id, first_seq, MsgType.ACK)
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
        self.slot.overwrite(wire(TEST_CONTROLLER_ID, agent.remote_id, 1, MsgType.ACK))
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
            wire(TEST_CONTROLLER_ID, agent.remote_id, 10, MsgType.PING)
        )
        _, ack = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ACK, 10)
        )
        msg = unpack(ack)
        assert msg is not None
        self.assertEqual(msg.frm, agent.remote_id)
        self.assertEqual(msg.to, TEST_CONTROLLER_ID)

    def test_ping_does_not_generate_response(self):
        """A PING produces only an ACK — no RESPONSE or ERROR follows."""
        agent = self.make_agent()
        self.slot.overwrite(
            wire(TEST_CONTROLLER_ID, agent.remote_id, 20, MsgType.PING)
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
        other_id = "Rcafef00"
        self.slot.overwrite(
            wire(TEST_CONTROLLER_ID, other_id, 1, MsgType.COMMAND, "not for me")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 1), timeout=0.05
            )

    def test_message_addressed_to_controller_is_ignored(self):
        """A message addressed to a controller ID is ignored by the agent (it's
        for the controller, not for us)."""
        self.make_agent()
        self.slot.overwrite(
            wire("Rcafef00", TEST_CONTROLLER_ID, 1, MsgType.RESPONSE, "not for me")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 1), timeout=0.05
            )


class TestAgentAnnounce(AgentTestCase):
    def test_announce_triggers_delayed_registration(self):
        """An ANNOUNCE from a controller causes the Agent to schedule a delayed
        registration response (a RESPONSE with sysinfo payload) to the
        controller."""
        import json

        agent = self.make_agent()
        # Deliver an ANNOUNCE from a controller
        self.slot.overwrite(
            wire(TEST_CONTROLLER_ID, BROADCAST_ADDR, 50, MsgType.ANNOUNCE,
                 json.dumps({"role": "controller", "name": "test", "version": 3}))
        )
        # The registration response arrives after a random delay (0.1–4.0s).
        # Wait up to 5s for a RESPONSE from the agent.
        _, reg = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 0), timeout=5.0
        )
        msg = unpack(reg)
        assert msg is not None
        self.assertEqual(msg.frm, agent.remote_id)
        self.assertEqual(msg.to, TEST_CONTROLLER_ID)
        # The payload should be sysinfo JSON with an "os" field.
        payload = json.loads(msg.payload)
        self.assertIn("os", payload)

    def test_announce_does_not_send_ack(self):
        """An ANNOUNCE does NOT produce an ACK — only the delayed registration
        response."""
        agent = self.make_agent()
        import json

        self.slot.overwrite(
            wire(TEST_CONTROLLER_ID, BROADCAST_ADDR, 60, MsgType.ANNOUNCE,
                 json.dumps({"role": "controller", "name": "test", "version": 3}))
        )
        # No ACK should appear within a short window
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 60), timeout=0.05
            )

    def test_broadcast_register_triggers_delayed_registration(self):
        """A legacy broadcast register command also causes the Agent to schedule
        a delayed registration response (backward compat)."""
        import json

        agent = self.make_agent()
        self.slot.overwrite(
            wire(TEST_CONTROLLER_ID, BROADCAST_ADDR, 70, MsgType.COMMAND,
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
        self.assertEqual(msg.to, TEST_CONTROLLER_ID)
        payload = json.loads(msg.payload)
        self.assertIn("os", payload)


class TestAgentHeartbeat(AgentTestCase):
    def make_heartbeat_agent(self, **overrides) -> Agent:
        """Agent whose heartbeat jitter is pinned to zero (deterministic timing)."""
        patcher = mock.patch("cliptunnel_mcp.agent._HEARTBEAT_JITTER_SECS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.make_agent(**overrides)

    def test_default_heartbeat_interval_is_120_seconds(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("CLIPTUNNEL_HEARTBEAT_SECS", None)
            agent = self.make_agent()
        self.assertEqual(agent.heartbeat_secs, 120.0)

    def test_heartbeat_env_var_overrides_interval(self):
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_HEARTBEAT_SECS": "0.05"}):
            agent = self.make_agent()
        self.assertEqual(agent.heartbeat_secs, 0.05)

    def test_heartbeat_env_var_accepts_float_values(self):
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_HEARTBEAT_SECS": "1.5"}):
            agent = self.make_agent()
        self.assertEqual(agent.heartbeat_secs, 1.5)

    def test_non_positive_heartbeat_interval_disables_thread(self):
        agent = self.make_agent(heartbeat_secs=0)
        self.assertLessEqual(agent.heartbeat_secs, 0)
        self.assertIsNone(agent._heartbeat_thread)

    def test_non_positive_env_var_disables_thread(self):
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_HEARTBEAT_SECS": "-1"}):
            agent = self.make_agent()
        self.assertIsNone(agent._heartbeat_thread)

    def test_heartbeat_sends_registrations_to_each_known_controller(self):
        agent = self.make_heartbeat_agent(heartbeat_secs=0.05)
        agent._known_controllers.update({TEST_CONTROLLER_ID, "C0ff3e55"})
        for cid in (TEST_CONTROLLER_ID, "C0ff3e55"):
            _, value = self.slot.wait_for_write(
                lambda v, c=cid: is_message(v, MsgType.RESPONSE, 0) and unpack(v).to == c,
                timeout=5.0,
            )
            msg = unpack(value)
            assert msg is not None
            self.assertEqual(msg.frm, agent.remote_id)
            payload = json.loads(msg.payload)
            self.assertIn("os", payload)

    def test_heartbeat_repeats_registrations_periodically(self):
        agent = self.make_heartbeat_agent(heartbeat_secs=0.05)
        agent._known_controllers.add(TEST_CONTROLLER_ID)
        def count_registrations() -> int:
            return sum(
                1
                for w in self.slot._writes
                if is_message(w, MsgType.RESPONSE, 0) and unpack(w).to == TEST_CONTROLLER_ID
            )

        self.assertTrue(
            wait_until(lambda: count_registrations() >= 3, timeout=5.0),
            "expected at least 3 periodic heartbeat registrations",
        )

    def test_heartbeat_skips_cycles_when_no_controllers_known(self):
        agent = self.make_heartbeat_agent(heartbeat_secs=0.02)
        current = self.slot.revision
        self.assertEqual(
            self.slot.wait_for_revision(after=current, timeout=0.2),
            current,
            "heartbeat must stay silent while no controller is known",
        )

    def test_heartbeat_thread_stops_on_close(self):
        agent = self.make_heartbeat_agent(heartbeat_secs=30.0)
        thread = agent._heartbeat_thread
        assert thread is not None
        self.assertTrue(thread.is_alive())
        agent.close()
        self.assertFalse(thread.is_alive())

    def test_heartbeat_survives_a_failing_registration_cycle(self):
        agent = self.make_heartbeat_agent(heartbeat_secs=0.05)
        agent._known_controllers.add(TEST_CONTROLLER_ID)
        original = agent.send_registration
        calls = {"n": 0}

        def flaky(controller_id=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient clipboard failure")
            return original(controller_id=controller_id)

        with mock.patch.object(agent, "send_registration", side_effect=flaky):
            self.assertTrue(
                wait_until(lambda: calls["n"] >= 2, timeout=5.0),
                "heartbeat thread must keep cycling after a failed registration",
            )
        self.slot.wait_for_write(
            lambda v: is_message(v, MsgType.RESPONSE, 0)
            and unpack(v).to == TEST_CONTROLLER_ID,
            timeout=5.0,
        )


if __name__ == "__main__":
    unittest.main()