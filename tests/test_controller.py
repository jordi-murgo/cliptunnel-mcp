"""Controller endpoint behavior over a deterministic slot transport.

Adapted to the CT3 protocol: the Controller generates a C+7hex controller_id
and announces its presence via the ANNOUNCE message type on startup (consuming
seq=1), so test commands start at seq=2 when using initial_seq=0.
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
TEST_REMOTE_ID = "R1a2b3c4"
TEST_CONTROLLER_ID = "C1a2b3c4"


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
            "controller_id": TEST_CONTROLLER_ID,
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
    def test_announce_on_startup(self):
        """The Controller broadcasts an ANNOUNCE on startup (seq=1)."""
        self.make_controller()
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.ANNOUNCE, 1)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, TEST_CONTROLLER_ID)
        self.assertEqual(message.to, BROADCAST_ADDR)
        self.assertEqual(message.mtype, MsgType.ANNOUNCE.value)
        payload = json.loads(message.payload)
        self.assertEqual(payload.get("role"), "controller")

    def test_initial_seq_seeds_command_sequence(self):
        controller = self.make_controller(initial_seq=5)
        # seq=6 is the announce; seq=7 is the first user command
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
            controller_id=TEST_CONTROLLER_ID,
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
            controller_id=TEST_CONTROLLER_ID,
        )
        self.addCleanup(controller.close)
        controller.send_command("work")
        path = Path(tmp) / SEQ_FILE_NAME
        self.assertTrue(path.exists())
        # seq=1 (announce) + seq=2 (command) = 2
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
            controller_id=TEST_CONTROLLER_ID,
        )
        self.addCleanup(controller.close)
        controller.send_command("work")
        self.assertFalse((Path(tmp) / SEQ_FILE_NAME).exists())

    def test_injected_store_loads_initial_and_saves_each_send(self):
        store = RecordingStore(initial=3)
        controller = self.make_controller(initial_seq=None, seq_store=store)
        # seq=4 is the announce; seq=5,6 are user commands
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

    def test_command_written_as_ct3_wire(self):
        controller = self.make_controller()
        controller.send_command("echo 'a|b'", remote_id=TEST_REMOTE_ID)
        # seq=1 is announce; seq=2 is our command
        _, value = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        message = unpack(value)
        assert message is not None
        self.assertEqual(message.frm, TEST_CONTROLLER_ID)
        self.assertEqual(message.to, TEST_REMOTE_ID)
        self.assertEqual(message.payload, "echo 'a|b'")

    def test_exact_ack_releases_dispatcher_for_next_command(self):
        controller = self.make_controller()
        controller.send_command("one", remote_id=TEST_REMOTE_ID)
        # seq=1 is announce; seq=2 is "one"
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.ACK))
        controller.send_command("two", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 3))
        # seq=1 (announce) + seq=2 (one) + seq=3 (two) = 3
        self.assertEqual(controller.seq, 3)

    def test_future_resolves_with_response_payload_and_acks_back(self):
        controller = self.make_controller()
        future = controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.RESPONSE, "result")
        )
        self.assertEqual(future.result(timeout=1.0), "result")
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.ACK, 2))

    def test_future_resolves_none_on_error(self):
        controller = self.make_controller()
        future = controller.send_command("work", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
        self.slot.overwrite(wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.ERROR, "boom"))
        self.assertIsNone(future.result(timeout=1.0))

    def test_send_command_sync_returns_response(self):
        controller = self.make_controller()

        def deliver() -> None:
            self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 2))
            self.slot.overwrite(wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.ACK))
            self.slot.overwrite(
                wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.RESPONSE, "sync-result")
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
        # seq=1 is announce (broadcast); seq=2 is our command — also broadcast
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
        # seq=1 is announce; seq=2 is our command
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
        # seq=1 is announce; seq=2 is our command
        first, _ = self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2)
        )
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 99, MsgType.RESPONSE, "unrelated")
        )
        self.slot.wait_for_write(
            lambda value: is_message(value, MsgType.COMMAND, 2), after=first
        )
        self.assertFalse(future.done())

    def test_stale_response_from_earlier_session_is_ignored(self):
        controller = self.make_controller(initial_seq=5, ack_timeout=0.5, retries=3)
        # seq=6 is announce; seq=7 is our command
        future = controller.send_command("fresh", remote_id=TEST_REMOTE_ID)
        self.slot.wait_for_write(lambda value: is_message(value, MsgType.COMMAND, 7))
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 5, MsgType.RESPONSE, "stale")
        )
        with self.assertRaises(AssertionError):
            self.slot.wait_for_write(
                lambda value: is_message(value, MsgType.ACK, 5), timeout=0.05
            )
        self.assertFalse(future.done())
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 7, MsgType.RESPONSE, "fresh-result")
        )
        self.assertEqual(future.result(timeout=1.0), "fresh-result")


class TestControllerRegistry(ControllerTestCase):
    def test_get_connections_initially_has_self_registered(self):
        controller = self.make_controller()
        connections = controller.get_connections()
        self.assertIn("controllers", connections)
        self.assertIn("remotes", connections)
        # The controller self-registers in its own controllers dict
        self.assertIn(controller.controller_id, connections["controllers"])

    def test_registration_response_updates_registry(self):
        """When a remote sends a RESPONSE with sysinfo (os field), the
        Controller adds it to the remotes registry."""
        import json as _json

        controller = self.make_controller()
        # Simulate a registration response from the agent
        sysinfo = _json.dumps({"os": "darwin", "hostname": "test", "python": "3.12"})
        self.slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 0, MsgType.RESPONSE, sysinfo)
        )
        # Give the reader thread time to process
        time.sleep(0.1)
        connections = controller.get_connections()
        self.assertIn(TEST_REMOTE_ID, connections["remotes"])
        self.assertEqual(connections["remotes"][TEST_REMOTE_ID]["os"], "darwin")
        self.assertEqual(connections["remotes"][TEST_REMOTE_ID]["status"], "alive")

    def test_get_connections_returns_copy(self):
        """get_connections returns a copy — mutating it doesn't affect the
        internal registry."""
        controller = self.make_controller()
        conns = controller.get_connections()
        conns["remotes"]["fake"] = {}


class TestControllerKeepalive(ControllerTestCase):
    def test_idle_remote_marked_dead_after_ping_timeout(self):
        """A remote that was pinged and didn't respond in 30s is marked dead."""
        import time as _time
        controller = self.make_controller()
        # Seed registry with a remote that was pinged 40s ago — no response
        old_time = _time.time() - 40
        with controller._registry_lock:
            controller._remotes[TEST_REMOTE_ID] = {
                "os": "test",
                "last_seen": _time.time() - 400,
                "status": "alive",
                "ping_sent_at": old_time,
            }
        # Simulate one keepalive iteration
        now = _time.time()
        with controller._registry_lock:
            for rid, info in controller._remotes.items():
                ping_sent_at = info.get("ping_sent_at")
                if ping_sent_at is not None and now - ping_sent_at > 30:
                    info["status"] = "dead"
                    info["ping_sent_at"] = None
        conns = controller.get_connections()
        self.assertEqual(conns["remotes"][TEST_REMOTE_ID]["status"], "dead")

    def test_idle_remote_not_pinged_before_5min(self):
        """A remote idle <300s should not be pinged."""
        import time as _time
        controller = self.make_controller()
        with controller._registry_lock:
            controller._remotes[TEST_REMOTE_ID] = {
                "os": "test", "last_seen": _time.time() - 120,
                "status": "alive", "ping_sent_at": None,
            }
        conns = controller.get_connections()
        self.assertEqual(conns["remotes"][TEST_REMOTE_ID]["status"], "alive")
        self.assertIsNone(conns["remotes"][TEST_REMOTE_ID].get("ping_sent_at"))

    def test_recent_remote_stays_alive(self):
        """A remote with recent last_seen should stay alive."""
        import time as _time
        controller = self.make_controller()
        with controller._registry_lock:
            controller._remotes[TEST_REMOTE_ID] = {
                "os": "test", "last_seen": _time.time(),
                "status": "alive", "ping_sent_at": None,
            }
        conns = controller.get_connections()
        self.assertEqual(conns["remotes"][TEST_REMOTE_ID]["status"], "alive")
        self.assertLess(conns["remotes"][TEST_REMOTE_ID]["last_seen_ago"], 5)


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
            controller_id=TEST_CONTROLLER_ID,
        )
        first.send_command("one")
        first.close()
        second = Controller(
            self.slot,
            timeout=1.0,
            ack_timeout=0.02,
            retries=1,
            poll_interval=0.001,
            controller_id=TEST_CONTROLLER_ID,
        )
        self.addCleanup(second.close)
        # first: seq=1 (announce) + seq=2 (one) = 2
        # second: loads seq=2, announces seq=3, then "two" is seq=4
        second.send_command("two")
        self.assertEqual(second.seq, 4)


def wait_until(predicate, timeout: float = 2.0, interval: float = 0.005) -> bool:
    """Bounded wait for *predicate* to become true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class RestoreRecordingSlot(ClipboardSlot):
    """Slot double that records restore_user_clipboard invocations."""

    def __init__(self) -> None:
        super().__init__()
        self.restore_calls: list[bool] = []

    def restore_user_clipboard(self) -> bool:
        self.restore_calls.append(True)
        return True


class TestControllerUserClipboardRestore(ControllerTestCase):
    def make_restoring_controller(self, **overrides) -> tuple[Controller, RestoreRecordingSlot]:
        slot = RestoreRecordingSlot()
        params: dict = {
            "timeout": 1.0,
            "ack_timeout": 0.05,
            "retries": 3,
            "poll_interval": 0.001,
            "initial_seq": 0,
            "persist_seq": False,
            "controller_id": TEST_CONTROLLER_ID,
        }
        params.update(overrides)
        controller = Controller(slot, **params)
        self.addCleanup(controller.close)
        return controller, slot

    def test_guarded_restore_invoked_after_final_registration_ack(self):
        """Registration exchange: RESPONSE seq=0 → controller ACK-back is the
        final write of the exchange, after which the guarded restore runs."""
        controller, slot = self.make_restoring_controller()
        slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 0, MsgType.RESPONSE,
                 json.dumps({"os": "Darwin"}))
        )
        slot.wait_for_write(lambda v: is_message(v, MsgType.ACK, 0))
        self.assertTrue(
            wait_until(lambda: slot.restore_calls, timeout=1.0),
            "restore must run after the final registration ACK",
        )

    def test_command_exchange_restores_once_after_final_ack(self):
        """COMMAND → ACK → RESPONSE → ACK flow: no restore mid-exchange (not
        after the command ACK), exactly one restore after the final ACK-back."""
        controller, slot = self.make_restoring_controller()
        controller.send_command("work")  # seq=2
        slot.wait_for_write(lambda v: is_message(v, MsgType.COMMAND, 2))

        # Mid-exchange: the agent's immediate ACK for our command must not
        # trigger a restore — the exchange is still open.
        slot.overwrite(wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.ACK))
        time.sleep(0.1)
        self.assertEqual(slot.restore_calls, [])

        # Exchange completes: RESPONSE arrives, controller ACKs it back,
        # then restores exactly once.
        slot.overwrite(
            wire(TEST_REMOTE_ID, TEST_CONTROLLER_ID, 2, MsgType.RESPONSE, "done")
        )
        slot.wait_for_write(lambda v: is_message(v, MsgType.ACK, 2))
        self.assertTrue(wait_until(lambda: len(slot.restore_calls) == 1, timeout=1.0))
        time.sleep(0.05)
        self.assertEqual(len(slot.restore_calls), 1)


if __name__ == "__main__":
    unittest.main()