"""Protocol contracts for a last-writer-wins half-duplex clipboard tunnel."""
from __future__ import annotations

import threading
import unittest

from cliptunnel_mcp.protocol import Message, MsgType, Role, pack, unpack
from cliptunnel_mcp.transport import Transport
from tests.clipboard_slot import ClipboardSlot


def wire(frm: Role, to: Role, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm.value, to=to.value, seq=seq, mtype=kind.value, payload=payload))


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
        self.slot.write(wire(Role.CONTROLLER, Role.AGENT, 1, MsgType.COMMAND, "work"))
        self.assertEqual(self.slot.revision, 1)
        self.slot.write(wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.ACK))
        self.assertEqual(self.slot.revision, 2)
        self.assertTrue(is_message(self.slot.read(), MsgType.ACK, 1))

    def test_overwrite_loses_previous_value_last_writer_wins(self):
        command = wire(Role.CONTROLLER, Role.AGENT, 1, MsgType.COMMAND, "work")
        stale = wire(Role.AGENT, Role.CONTROLLER, 99, MsgType.RESPONSE, "unrelated")
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

    def test_wait_for_write_observes_ct1_wire_message(self):
        command = wire(Role.CONTROLLER, Role.AGENT, 7, MsgType.COMMAND, "echo 'a|b'")
        self.slot.write(command)
        count, observed = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 7)
        )
        self.assertEqual(count, 1)
        self.assertEqual(observed, command)

    def test_wait_for_write_scans_only_after_index(self):
        first = wire(Role.CONTROLLER, Role.AGENT, 1, MsgType.COMMAND)
        second = wire(Role.CONTROLLER, Role.AGENT, 2, MsgType.COMMAND)
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
        )
        self.addCleanup(controller.close)
        return controller

    def test_unrelated_response_does_not_acknowledge_current_command(self):
        """send_command('current') writes CT1 COMMAND seq=1 (C→A); the slot is
        overwritten by an Agent→Controller RESPONSE seq=99 ('unrelated'). The
        unrelated response must not acknowledge the pending command: the
        Controller retransmits COMMAND seq=1 as a later slot write."""
        controller = self.make_controller()
        controller.send_command("current")
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )

        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 99, MsgType.RESPONSE, "unrelated")
        )

        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1), after=first
        )

    def test_older_response_overwriting_unread_command_causes_command_retry(self):
        """COMMAND seq=1 'first' pending → ACK seq=1 → COMMAND seq=2 'second'
        written → an older RESPONSE seq=1 'first-result' overwrites the unread
        COMMAND seq=2. The Controller must resolve seq=1 with 'first-result'
        AND retransmit COMMAND seq=2 afterwards."""
        controller = self.make_controller()
        first_future = controller.send_command("first")
        first_write, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )
        self.slot.overwrite(wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.ACK))
        controller.send_command("second")
        second_write, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=first_write
        )

        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.RESPONSE, "first-result")
        )
        self.assertEqual(first_future.result(timeout=1), "first-result")

        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=second_write
        )

    def test_same_sequence_response_safely_subsumes_command_ack(self):
        """COMMAND seq=1 'fast' is answered directly by RESPONSE seq=1 'done'
        (no explicit ACK observed first): the same-seq R subsumes the command
        ACK and send_command's future resolves to 'done'."""
        controller = self.make_controller()
        future = controller.send_command("fast")
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )

        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.RESPONSE, "done")
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

    def deliver_command(self, seq: int, payload: str = "work") -> None:
        self.slot.overwrite(
            wire(Role.CONTROLLER, Role.AGENT, seq, MsgType.COMMAND, payload)
        )

    def test_new_command_does_not_acknowledge_unobserved_pending_response(self):
        """The Agent answers COMMAND seq=1 with RESPONSE seq=1; before the
        Controller's ACK is observed, COMMAND seq=2 arrives. The pending
        RESPONSE seq=1 must be retransmitted byte-identically, not dropped
        by the new command."""
        agent = self.make_agent()
        self.deliver_command(1, "one")
        response_write, response = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1)
        )

        self.deliver_command(2, "two")

        _, retried = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.RESPONSE, 1), after=response_write
        )
        self.assertEqual(retried, response)

    def test_lost_response_ack_retransmits_exact_response(self):
        """RESPONSE seq=1 is written; its ACK is never observed; the Agent
        retransmits the exact same wire string (bounded wait sees a second
        identical RESPONSE write)."""
        agent = self.make_agent()
        self.deliver_command(1)
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
        self.deliver_command(1)
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
        self.deliver_command(1)
        first_error, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1)
        )
        self.slot.overwrite(wire(Role.CONTROLLER, Role.AGENT, 1, MsgType.ACK))
        self.deliver_command(1)

        _, duplicate = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ERROR, 1), after=first_error
        )
        self.assertEqual(unpack(duplicate).mtype, MsgType.ERROR.value)


if __name__ == "__main__":
    unittest.main()
