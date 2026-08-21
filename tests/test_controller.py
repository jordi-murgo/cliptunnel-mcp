"""Controller endpoint behavior over a deterministic slot transport.

Adapted to the CT2 protocol: the Controller uses CONTROLLER_ADDR="C" and
targets remotes by their 8-hex remote_id. The Controller broadcasts a register
command on startup (consuming seq=1), so test commands start at seq=2 when
using initial_seq=0.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from cliptunnel_mcp import Controller
from cliptunnel_mcp.controller import FileSeqStore
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    Message,
    MsgType,
    pack,
    unpack,
)
from tests.clipboard_slot import ClipboardSlot

SEQ_FILE_NAME = ".cliptunnel_controller_seq"
TEST_REMOTE_ID = "deadbeef"


def wire(frm: str, to: str, seq: int, kind: MsgType, payload: str = "") -> str:
    return pack(Message(frm=frm, to=to, seq=seq, mtype=kind.value, payload=payload))


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
            "persist_seq": False,
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
    def test_broadcast_register_on_startup(self):
        """The Controller broadcasts a register command on startup (seq=1)."""
        self.make_controller()
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 1)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, CONTROLLER_ADDR)
        self.assertEqual(message.to, BROADCAST_ADDR)
        payload = json.loads(message.payload)
        self.assertEqual(payload.get("op"), "register")

    def test_initial_seq_seeds_command_sequence(self):
        controller = self.make_controller(initial_seq=5)
        # seq=6 is the broadcast register; seq=7 is the first user command
        controller.send_command("work")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 7))

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
        # seq=1 (register) + seq=2 (command) = 2
        self.assertEqual(json.loads(path.read_text("utf-8"))["seq"], 2)

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
        # seq=4 is the broadcast register; seq=5,6 are user commands
        controller.send_command("one")
        controller.send_command("two")
        self.assertEqual(store.saves, [4, 5, 6])


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

    def test_command_written_as_ct2_wire(self):
        controller = self.make_controller()
        controller.send_command("echo 'a|b'", remote_id=TEST_REMOTE_ID)
        # seq=1 is broadcast register; seq=2 is our command
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, CONTROLLER_ADDR)
        self.assertEqual(message.to, TEST_REMOTE_ID)
        self.assertEqual(message.payload, "echo 'a|b'")

    def test_exact_ack_releases_dispatcher_for_next_command(self):
        controller = self.make_controller()
        controller.send_command("one", remote_id=TEST_REMOTE_ID)
        # seq=1 is register; seq=2 is "one"
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.ACK))
        controller.send_command("two", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 3))
        # seq=1 (register) + seq=2 (one) + seq=3 (two) = 3
        self.assertEqual(controller.seq, 3)

    def test_future_resolves_with_response_payload_and_acks_back(self):
        controller = self.make_controller()
        future = controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.RESPONSE, "result")
        )
        self.assertEqual(future.result(timeout=1.0), "result")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 2))

    def test_future_resolves_none_on_error(self):
        controller = self.make_controller()
        future = controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.ERROR, "boom"))
        self.assertIsNone(future.result(timeout=1.0))

    def test_send_command_sync_returns_response(self):
        controller = self.make_controller()

        def deliver() -> None:
            self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
            self.slot.overwrite(wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.ACK))
            self.slot.overwrite(
                wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 2, MsgType.RESPONSE, "sync-result")
            )

        writer = threading.Thread(target=deliver, daemon=True)
        writer.start()
        self.assertEqual(
            controller.send_command_sync("work", remote_id=TEST_REMOTE_ID), "sync-result"
        )
        writer.join(timeout=1.0)

    def test_send_command_without_remote_id_uses_broadcast(self):
        """When no remote_id is given and no remotes are registered, the
        command is sent to the broadcast address."""
        controller = self.make_controller()
        controller.send_command("work")
        # seq=1 is register (broadcast); seq=2 is our command — also broadcast
        # since no remotes registered yet
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.to, BROADCAST_ADDR)


class TestControllerRetryOnAckTimeout(ControllerTestCase):
    def test_retransmits_command_on_ack_timeout(self):
        controller = self.make_controller(ack_timeout=0.02, retries=3)
        controller.send_command("work", remote_id=TEST_REMOTE_ID)
        # seq=1 is register; seq=2 is our command
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=first
        )

    def test_future_resolves_none_when_retries_exhausted(self):
        controller = self.make_controller(ack_timeout=0.02, retries=2)
        future = controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.assertIsNone(future.result(timeout=1.0))


class TestControllerDedupe(ControllerTestCase):
    def test_unrelated_response_does_not_release_pending_command(self):
        controller = self.make_controller(ack_timeout=0.02, retries=8)
        future = controller.send_command("current", remote_id=TEST_REMOTE_ID)
        # seq=1 is register; seq=2 is our command
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 99, MsgType.RESPONSE, "unrelated")
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=first
        )
        self.assertFalse(future.done())

    def test_stale_response_from_earlier_session_is_ignored(self):
        controller = self.make_controller(initial_seq=5, ack_timeout=0.5, retries=3)
        # seq=6 is register; seq=7 is our command
        future = controller.send_command("fresh", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 7))
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 5, MsgType.RESPONSE, "stale")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 5), timeout=0.05
            )
        self.assertFalse(future.done())
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 7, MsgType.RESPONSE, "fresh-result")
        )
        self.assertEqual(future.result(timeout=1.0), "fresh-result")


class TestControllerRegistry(ControllerTestCase):
    def test_get_connections_returns_empty_dict_initially(self):
        controller = self.make_controller()
        self.assertEqual(controller.get_connections(), {})

    def test_registration_response_updates_registry(self):
        """When a remote sends a RESPONSE with sysinfo (os field), the
        Controller adds it to the registry."""
        import json as _json

        controller = self.make_controller()
        # Simulate a registration response from the agent
        sysinfo = _json.dumps({"os": "darwin", "hostname": "test", "python": "3.12"})
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, CONTROLLER_ADDR, 0, MsgType.RESPONSE, sysinfo)
        )
        # Give the reader thread time to process
        time.sleep(0.1)
        connections = controller.get_connections()
        self.assertIn(TEST_REMOTE_ID, connections)
        self.assertEqual(connections[TEST_REMOTE_ID]["os"], "darwin")
        self.assertEqual(connections[TEST_REMOTE_ID]["status"], "alive")

    def test_get_connections_returns_copy(self):
        """get_connections returns a copy — mutating it doesn't affect the
        internal registry."""
        controller = self.make_controller()
        conns = controller.get_connections()
        conns["fake"] = {}
        self.assertNotIn("fake", controller.get_connections())


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
        controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
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
        # first: seq=1 (register) + seq=2 (one) = 2
        # second: loads seq=2, broadcasts register seq=3, then "two" is seq=4
        second.send_command("two")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 4))
        self.assertEqual(second.seq, 4)


if __name__ == "__main__":
    unittest.main()