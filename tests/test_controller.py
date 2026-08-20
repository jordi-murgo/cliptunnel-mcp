"""Controller endpoint behavior over a deterministic slot transport.

Ported from vulcano-helper tests/test_host_async.py and the HostSlotContracts
of tests/test_clipboard_transport.py, adapted to the CT1 {C, A} role alphabet
and constructor-injected transports (no clipboard mocking required).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from cliptunnel_mcp import Controller
from cliptunnel_mcp.controller import FileSeqStore
from cliptunnel_mcp.protocol import Message, MsgType, Role, pack, unpack
from tests.clipboard_slot import ClipboardSlot

SEQ_FILE_NAME = ".cliptunnel_controller_seq"


def wire(frm: Role, to: Role, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm.value, to=to.value, seq=seq, mtype=kind.value, payload=payload))


def is_message(value: str, kind: MsgType, seq: int) -> bool:
    message = unpack(value)
    return message is not None and message.mtype == kind.value and message.seq == seq


class RecordingStore:
    """Injectable seq store recording save calls."""

    def __init__(self, initial: int = 0) -> None:
        self.initial = initial
        self.saves: list[int] = []

    def load(self) -> int:
        return self.initial

    def save(self, seq: int) -> None:
        self.saves.append(seq)


class ControllerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.slot = ClipboardSlot()

    def make_controller(self, **overrides) -> Controller:
        params: dict = {
            "timeout": 1.0,
            "ack_timeout": 0.05,
            "retries": 3,
            "poll_interval": 0.001,
            "initial_seq": 0,
        }
        params.update(overrides)
        controller = Controller(self.slot, **params)
        self.addCleanup(controller.close)
        return controller

    def chdir_tmp(self) -> str:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        previous = os.getcwd()
        os.chdir(tmp)
        self.addCleanup(os.chdir, previous)
        return tmp


class TestControllerConstruction(ControllerTestCase):
    def test_constructor_with_injected_slot_writes_nothing(self):
        self.make_controller()
        self.assertEqual(
            self.slot.wait_for_revision(after=self.slot.revision, timeout=0.02),
            self.slot.revision,
        )
        self.assertEqual(self.slot.read(), "")

    def test_initial_seq_seeds_command_sequence(self):
        controller = self.make_controller(initial_seq=5)
        controller.send_command("work")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 6))

    def test_initial_seq_disables_default_file_store(self):
        tmp = self.chdir_tmp()
        controller = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
            initial_seq=0,
        )
        self.addCleanup(controller.close)
        controller.send_command("work")
        self.assertFalse((Path(tmp) / SEQ_FILE_NAME).exists())

    def test_default_store_persists_seq_file(self):
        tmp = self.chdir_tmp()
        controller = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
        )
        self.addCleanup(controller.close)
        controller.send_command("work")
        path = Path(tmp) / SEQ_FILE_NAME
        self.assertTrue(path.exists())
        self.assertEqual(json.loads(path.read_text("utf-8"))["seq"], 1)

    def test_persist_seq_false_writes_no_file(self):
        tmp = self.chdir_tmp()
        controller = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
            persist_seq=False,
        )
        self.addCleanup(controller.close)
        controller.send_command("work")
        self.assertFalse((Path(tmp) / SEQ_FILE_NAME).exists())

    def test_injected_store_loads_initial_and_saves_each_send(self):
        store = RecordingStore(initial=3)
        controller = self.make_controller(initial_seq=None, seq_store=store)
        controller.send_command("one")
        controller.send_command("two")
        self.assertEqual(store.saves, [4, 5])


class TestFileSeqStore(unittest.TestCase):
    def test_save_then_load_round_trips(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            store = FileSeqStore(Path(tmp) / "seq.json")
            store.save(41)
            self.assertEqual(store.load(), 41)

    def test_missing_file_loads_zero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(FileSeqStore(Path(tmp) / "absent.json").load(), 0)

    def test_corrupt_file_loads_zero(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seq.json"
            path.write_text('{"seq": "not-a-number"}', "utf-8")
            self.assertEqual(FileSeqStore(path).load(), 0)


class TestControllerSendCommand(ControllerTestCase):
    def test_send_command_returns_unresolved_future(self):
        controller = self.make_controller()
        future = controller.send_command("work")
        self.assertIsInstance(future, concurrent.futures.Future)
        self.assertFalse(future.done())

    def test_command_written_as_ct1_wire(self):
        controller = self.make_controller()
        controller.send_command("echo 'a|b'")
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, Role.CONTROLLER.value)
        self.assertEqual(message.to, Role.AGENT.value)
        self.assertEqual(message.payload, "echo 'a|b'")

    def test_exact_ack_releases_dispatcher_for_next_command(self):
        controller = self.make_controller()
        controller.send_command("one")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 1))
        self.slot.overwrite(wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.ACK))
        controller.send_command("two")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.assertEqual(controller.seq, 2)

    def test_future_resolves_with_response_payload_and_acks_back(self):
        controller = self.make_controller()
        future = controller.send_command("work")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 1))
        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.RESPONSE, "result")
        )
        self.assertEqual(future.result(timeout=1.0), "result")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 1))

    def test_future_resolves_none_on_error(self):
        controller = self.make_controller()
        future = controller.send_command("work")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 1))
        self.slot.overwrite(wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.ERROR, "boom"))
        self.assertIsNone(future.result(timeout=1.0))

    def test_send_command_sync_returns_response(self):
        controller = self.make_controller()

        def deliver() -> None:
            self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 1))
            self.slot.overwrite(wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.ACK))
            self.slot.overwrite(
                wire(Role.AGENT, Role.CONTROLLER, 1, MsgType.RESPONSE, "sync-result")
            )

        writer = threading.Thread(target=deliver, daemon=True)
        writer.start()
        self.assertEqual(controller.send_command_sync("work"), "sync-result")
        writer.join(timeout=1.0)


class TestControllerRetryOnAckTimeout(ControllerTestCase):
    def test_retransmits_command_on_ack_timeout(self):
        controller = self.make_controller(ack_timeout=0.02, retries=3)
        controller.send_command("work")
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1), after=first
        )

    def test_future_resolves_none_when_retries_exhausted(self):
        controller = self.make_controller(ack_timeout=0.02, retries=2)
        future = controller.send_command("work")
        self.assertIsNone(future.result(timeout=1.0))


class TestControllerDedupe(ControllerTestCase):
    def test_unrelated_response_does_not_release_pending_command(self):
        controller = self.make_controller(ack_timeout=0.02, retries=8)
        future = controller.send_command("current")
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )
        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 99, MsgType.RESPONSE, "unrelated")
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1), after=first
        )
        self.assertFalse(future.done())

    def test_stale_response_from_earlier_session_is_ignored(self):
        controller = self.make_controller(initial_seq=5, ack_timeout=0.5, retries=3)
        future = controller.send_command("fresh")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 6))
        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 5, MsgType.RESPONSE, "stale")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 5), timeout=0.05
            )
        self.assertFalse(future.done())
        self.slot.overwrite(
            wire(Role.AGENT, Role.CONTROLLER, 6, MsgType.RESPONSE, "fresh-result")
        )
        self.assertEqual(future.result(timeout=1.0), "fresh-result")


class TestControllerClose(ControllerTestCase):
    def test_close_is_idempotent_and_stops_threads(self):
        controller = self.make_controller()
        controller.close()
        controller.close()
        self.assertFalse(controller._running)
        self.assertFalse(controller._dispatcher_thread.is_alive())
        self.assertFalse(controller._reader_thread.is_alive())

    def test_close_wakes_dispatcher_from_ack_wait(self):
        controller = self.make_controller(ack_timeout=5.0)
        controller.send_command("work")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 1))
        controller.close()
        self.assertFalse(controller._dispatcher_thread.is_alive())
        self.assertFalse(controller._reader_thread.is_alive())

    def test_restart_continues_seq_from_persisted_store(self):
        self.chdir_tmp()
        first = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
        )
        first.send_command("one")
        first.close()
        second = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
        )
        self.addCleanup(second.close)
        second.send_command("two")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.assertEqual(second.seq, 2)


if __name__ == "__main__":
    unittest.main()
