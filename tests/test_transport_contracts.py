"""Protocol contracts for a last-writer-wins half-duplex clipboard tunnel."""
from __future__ import annotations

import threading
import unittest

from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    Message,
    MsgType,
    pack,
    unpack,
)
from cliptunnel_mcp.transport import Transport
from tests.clipboard_slot import ClipboardSlot

# Fixed test remote ID — 8-char hex, used everywhere an agent address is needed.
TEST_REMOTE_ID = "deadbeef"


def wire(frm: str, to: str, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm, to=to, seq=seq, mtype=kind.value, payload=payload))


def is_message(value: str, kind: MsgType, seq: int) -> bool:
    message = unpack(value)
    return message is not None and message.mtype == kind.value and message.seq == seq


class ClipboardSlotContracts(unittest.TestCase):
    """The deterministic fake slot: single value, revisions, bounded waits."""

    def setUp(self) -> None:
        self.slot = ClipboardSlot()

    def test_starts_empty_at_revision_zero(self):
        self.assertEqual(self.slot.read(), "")
        self.assertEqual(self.slot.revision, 0)

    def test_write_replaces_value_and_bumps_revision(self):
        self.slot.write(wire(CONTROLLER_ADDR, TEST_REMOTE_ID, 1, MsgType.COMMAND, "work"))
        self.assertEqual(self.slot.revision, 1)
        self.slot.write(wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 1, MsgType.ACK))
        self.assertEqual(self.slot.revision, 2)
        self.assertTrue(is_message(self.slot.read(), MsgType.ACK, 1))

    def test_overwrite_loses_previous_value_last_writer_wins(self):
        command = wire(CONTROLLER_ADDR, TEST_REMOTE_ID, 1, MsgType.COMMAND, "work")
        stale = wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 99, MsgType.RESPONSE, "unrelated")
        self.slot.write(command)
        self.slot.overwrite(stale)
        self.assertEqual(self.slot.read(), stale)
        self.assertNotEqual(self.slot.read(), command)

    def test_wait_for_revision_returns_once_revision_advances(self):
        writer = threading.Thread(target=lambda: self.slot.write("later"), daemon=True)
        writer.start()
        revision = self.slot.wait_for_revision(after=0, timeout=2.0)
        self.assertGreaterEqual(revision, 1)
        writer.join(timeout=1.0)

    def test_wait_for_revision_timeout_returns_current_revision(self):
        self.slot.write("now")
        current = self.slot.revision
        self.assertEqual(self.slot.wait_for_revision(after=current, timeout=0.05), current)

    def test_wait_for_write_observes_ct2_wire_message(self):
        command = wire(CONTROLLER_ADDR, TEST_REMOTE_ID, 7, MsgType.COMMAND, "echo 'a|b'")
        self.slot.write(command)
        count, observed = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 7)
        )
        self.assertEqual(count, 1)
        self.assertEqual(observed, command)

    def test_wait_for_write_scans_only_after_index(self):
        first = wire(CONTROLLER_ADDR, TEST_REMOTE_ID, 1, MsgType.COMMAND)
        second = wire(CONTROLLER_ADDR, TEST_REMOTE_ID, 2, MsgType.COMMAND)
        self.slot.write(first)
        self.slot.write(second)
        count, observed = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        self.assertEqual((count, observed), (2, second))
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.COMMAND, 2), after=2, timeout=0.05
            )

    def test_wait_for_write_raises_when_no_write_matches(self):
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(lambda value: False, timeout=0.05)

    def test_slot_satisfies_transport_protocol(self):
        self.assertIsInstance(self.slot, Transport)


class ControllerSlotContracts(unittest.TestCase):
    """The Controller endpoint over the fake slot (vulcano HostSlotContracts)."""

    def setUp(self) -> None:
        self.slot = ClipboardSlot()

    def make_controller(self):
        from cliptunnel_mcp import Controller

        controller = Controller(
            self.slot,
            timeout=1,
            ack_timeout=0.03,
            retries=3,
            poll_interval=0.001,
            initial_seq=0,
            persist_seq=False,
        )
        self.addCleanup(controller.close)
        return controller

    def test_unrelated_response_does_not_acknowledge_current_command(self):
        """send_command('current') writes CT2 COMMAND seq=2 (C→remote); the slot
        is overwritten by a remote→Controller RESPONSE seq=99 ('unrelated'). The
        unrelated response must not acknowledge the pending command: the
        Controller retransmits COMMAND seq=2 as a later slot write."""
        controller = self.make_controller()
        # seq=1 is consumed by the broadcast register on startup.
        controller.send_command("current")
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )

        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 99, MsgType.RESPONSE, "unrelated")
        )

        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=first
        )

    def test_older_response_overwriting_unread_command_causes_command_retry(self):
        """COMMAND seq=2 'first' pending → ACK seq=2 → COMMAND seq=3 'second'
        written → an older RESPONSE seq=2 'first-result' overwrites the unread
        COMMAND seq=3. The Controller must resolve seq=2 with 'first-result'
        AND retransmit COMMAND seq=3 afterwards."""
        controller = self.make_controller()
        first_future = controller.send_command("first")
        first_write, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        self.slot.overwrite(wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.ACK))
        controller.send_command("second")
        second_write, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 3), after=first_write
        )

        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.RESPONSE, "first-result")
        )
        self.assertEqual(first_future.result(timeout=1), "first-result")

        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 3), after=second_write
        )

    def test_same_sequence_response_safely_subsumes_command_ack(self):
        """COMMAND seq=2 'fast' is answered directly by RESPONSE seq=2 'done'
        (no explicit ACK observed first): the same-seq R subsumes the command
        ACK and send_command's future resolves to 'done'."""
        controller = self.make_controller()
        future = controller.send_command("fast")
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )

        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.RESPONSE, "done")
        )

        self.assertEqual(future.result(timeout=1), "done")


class AgentSlotContracts(unittest.TestCase):
    """The Agent endpoint over the fake slot (vulcano VDISlotContracts)."""

    def setUp(self) -> None:
        self.slot = ClipboardSlot()

    def make_agent(self, *, error: bool = False):
        from cliptunnel_mcp import Agent

        agent = Agent(
            self.slot,
            lambda payload: (f"result:{payload}", error),
            poll_interval=0.001,
            max_workers=1,
            response_ack_timeout=0.02,
        )
        self.addCleanup(agent.close)
        return agent
    def deliver_command(self, agent, seq: int, payload: str = "work") -> None:
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, agent.remote_id, seq, MsgType.COMMAND, payload)
        )

    def test_new_command_does_not_acknowledge_unobserved_pending_response(self):
        """The Agent answers COMMAND seq=1 with RESPONSE seq=1; before the
        Controller's ACK is observed, COMMAND seq=2 arrives. The pending
        RESPONSE seq=1 must be retransmitted byte-identically, not dropped
        by the new command."""
        agent = self.make_agent()
        self.deliver_command(agent, 1, "one")
        response_write, response = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )

        self.deliver_command(agent, 2, "two")

        _, retried = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1), after=response_write
        )
        self.assertEqual(retried, response)

    def test_lost_response_ack_retransmits_exact_response(self):
        """RESPONSE seq=1 is written; its ACK is never observed; the Agent
        retransmits the exact same wire string (bounded wait sees a second
        identical RESPONSE write)."""
        agent = self.make_agent()
        self.deliver_command(agent, 1)
        response_write, response = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )

        _, retry = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1), after=response_write
        )
        self.assertEqual(retry, response)

    def test_lost_error_ack_retransmits_exact_error(self):
        """Same as the response case, but the handler fails: ERROR seq=1 is
        retransmitted byte-identically while its ACK is unobserved."""
        agent = self.make_agent(error=True)
        self.deliver_command(agent, 1)
        error_write, error = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )

        _, retry = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1), after=error_write
        )
        self.assertEqual(retry, error)

    def test_duplicate_cached_error_remains_error(self):
        """COMMAND seq=1 processed with an error, ACK delivered, then the
        duplicate COMMAND seq=1 is re-delivered: the Agent replays the cached
        ERROR (typed cache preserves E-ness), never a fresh RESPONSE."""
        agent = self.make_agent(error=True)
        self.deliver_command(agent, 1)
        first_error, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )
        self.slot.overwrite(wire(CONTROLLER_ADDR, agent.remote_id, 1, MsgType.ACK))
        self.deliver_command(agent, 1)

        _, duplicate = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1), after=first_error
        )
        self.assertEqual(unpack(duplicate).mtype, MsgType.ERROR.value)

    def test_ping_is_acked_immediately(self):
        """A PING message to the agent is answered with an ACK immediately,
        without processing or generating a response."""
        agent = self.make_agent()
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, agent.remote_id, 10, MsgType.PING)
        )
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 10))

    def test_message_addressed_to_different_remote_is_ignored(self):
        """A message with to=different_hex_id is ignored by this agent."""
        self.make_agent()
        other_id = "cafef00d"
        # Write a command addressed to a different remote
        self.slot.overwrite(
            wire(CONTROLLER_ADDR, other_id, 1, MsgType.COMMAND, "not for me")
        )
        # No ACK should appear within a short window
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 1), timeout=0.05
            )

    def test_broadcast_register_triggers_delayed_registration(self):
        """A broadcast register command causes the Agent to schedule a delayed
        registration response (a RESPONSE with sysinfo payload) to the
        controller."""
        import json

        agent = self.make_agent()
        # Deliver a broadcast register command
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
        self.assertIsNotNone(msg)
        self.assertEqual(msg.frm, agent.remote_id)
        self.assertEqual(msg.to, CONTROLLER_ADDR)
        # The payload should be sysinfo JSON with an "os" field.
        payload = json.loads(msg.payload)
        self.assertIn("os", payload)


if __name__ == "__main__":
    unittest.main()