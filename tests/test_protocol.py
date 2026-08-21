"""Unit tests for cliptunnel_mcp.protocol — stdlib unittest, zero deps."""
from __future__ import annotations

import unittest

import cliptunnel_mcp
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    PROTOCOL_SIG,
    PROTOCOL_VERSION,
    Message,
    MsgType,
    SeqTracker,
    generate_remote_id,
    is_broadcast,
    is_controller,
    is_valid_address,
    pack,
    unpack,
    validate,
)

# Fixed test remote ID — 8-char hex, used everywhere an agent address is needed.
TEST_REMOTE_ID = "deadbeef"


class TestPackageIdentity(unittest.TestCase):
    def test_version(self):
        from importlib.metadata import version

        self.assertEqual(cliptunnel_mcp.__version__, version("cliptunnel-mcp"))

    def test_reexports_protocol_surface(self):
        self.assertIs(cliptunnel_mcp.pack, pack)
        self.assertIs(cliptunnel_mcp.unpack, unpack)
        self.assertIs(cliptunnel_mcp.validate, validate)
        self.assertIs(cliptunnel_mcp.Message, Message)
        self.assertIs(cliptunnel_mcp.MsgType, MsgType)
        self.assertIs(cliptunnel_mcp.SeqTracker, SeqTracker)
        self.assertIs(cliptunnel_mcp.generate_remote_id, generate_remote_id)
        self.assertIs(cliptunnel_mcp.CONTROLLER_ADDR, CONTROLLER_ADDR)
        self.assertIs(cliptunnel_mcp.BROADCAST_ADDR, BROADCAST_ADDR)


class TestConstants(unittest.TestCase):
    def test_controller_addr(self):
        self.assertEqual(CONTROLLER_ADDR, "C")

    def test_broadcast_addr(self):
        self.assertEqual(BROADCAST_ADDR, "*")

    def test_protocol_sig(self):
        self.assertEqual(PROTOCOL_SIG, "CT2")

    def test_protocol_version(self):
        self.assertEqual(PROTOCOL_VERSION, 2)


class TestMsgTypeEnumValues(unittest.TestCase):
    def test_command(self):
        self.assertEqual(MsgType.COMMAND.value, "C")

    def test_response(self):
        self.assertEqual(MsgType.RESPONSE.value, "R")

    def test_error(self):
        self.assertEqual(MsgType.ERROR.value, "E")

    def test_ack(self):
        self.assertEqual(MsgType.ACK.value, "A")

    def test_ping(self):
        self.assertEqual(MsgType.PING.value, "P")


class TestMessageConstruction(unittest.TestCase):
    def test_message_fields(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls -la")
        self.assertEqual(m.frm, "C")
        self.assertEqual(m.to, "deadbeef")
        self.assertEqual(m.seq, 1)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls -la")

    def test_message_fields_are_plain_strings(self):
        m = Message(frm=TEST_REMOTE_ID, to=CONTROLLER_ADDR, seq=99, mtype="R", payload="ok")
        self.assertIsInstance(m.frm, str)
        self.assertIsInstance(m.to, str)


class TestGenerateRemoteId(unittest.TestCase):
    def test_generates_8_hex_chars(self):
        rid = generate_remote_id()
        self.assertEqual(len(rid), 8)
        self.assertTrue(is_valid_address(rid))

    def test_generates_different_ids(self):
        ids = {generate_remote_id() for _ in range(100)}
        # Collisions in 100 random 32-bit IDs are astronomically unlikely.
        self.assertGreater(len(ids), 90)

    def test_generated_id_is_lowercase_hex(self):
        rid = generate_remote_id()
        self.assertTrue(all(c in "0123456789abcdef" for c in rid))


class TestIsValidAddress(unittest.TestCase):
    def test_controller_addr_valid(self):
        self.assertTrue(is_valid_address(CONTROLLER_ADDR))

    def test_broadcast_addr_valid(self):
        self.assertTrue(is_valid_address(BROADCAST_ADDR))

    def test_8_char_hex_valid(self):
        self.assertTrue(is_valid_address("deadbeef"))
        self.assertTrue(is_valid_address("00000000"))
        self.assertTrue(is_valid_address("ffffffff"))

    def test_short_hex_invalid(self):
        self.assertFalse(is_valid_address("dead"))
        self.assertFalse(is_valid_address("deadbee"))

    def test_long_hex_invalid(self):
        self.assertFalse(is_valid_address("deadbeef00"))

    def test_uppercase_hex_invalid(self):
        self.assertFalse(is_valid_address("DeadBeef"))

    def test_non_hex_invalid(self):
        self.assertFalse(is_valid_address("xyzw1234"))

    def test_empty_invalid(self):
        self.assertFalse(is_valid_address(""))


class TestIsController(unittest.TestCase):
    def test_controller_addr(self):
        self.assertTrue(is_controller(CONTROLLER_ADDR))
        self.assertTrue(is_controller("C"))

    def test_non_controller(self):
        self.assertFalse(is_controller(TEST_REMOTE_ID))
        self.assertFalse(is_controller(BROADCAST_ADDR))


class TestIsBroadcast(unittest.TestCase):
    def test_broadcast_addr(self):
        self.assertTrue(is_broadcast(BROADCAST_ADDR))
        self.assertTrue(is_broadcast("*"))

    def test_non_broadcast(self):
        self.assertFalse(is_broadcast(CONTROLLER_ADDR))
        self.assertFalse(is_broadcast(TEST_REMOTE_ID))


class TestPack(unittest.TestCase):
    def test_pack_basic(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=41, mtype="C", payload="ls")
        result = pack(m)
        self.assertTrue(result.startswith("CT2|C|deadbeef|41|C|"))
        # payload "ls" base64 = "bHM="
        self.assertEqual(result, "CT2|C|deadbeef|41|C|bHM=")

    def test_pack_unicode(self):
        m = Message(frm=TEST_REMOTE_ID, to=CONTROLLER_ADDR, seq=1, mtype="R", payload="cañón")
        result = pack(m)
        self.assertTrue(result.startswith("CT2|deadbeef|C|1|R|"))
        # payload must be base64-encoded (no raw unicode in wire format)
        self.assertNotIn("cañón", result)

    def test_pack_special_chars(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="echo 'a|b|c'")
        result = pack(m)
        # pipe chars in payload must not appear raw (they're base64-encoded)
        parts = result.split("|")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "CT2")

    def test_pack_empty_payload(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="")
        self.assertEqual(pack(m), "CT2|C|deadbeef|1|C|")

    def test_pack_ack_empty_payload(self):
        m = Message(frm=TEST_REMOTE_ID, to=CONTROLLER_ADDR, seq=41, mtype="A", payload="")
        result = pack(m)
        self.assertTrue(result.startswith("CT2|deadbeef|C|41|A|"))
        # empty payload base64 = ""
        self.assertEqual(result, "CT2|deadbeef|C|41|A|")

    def test_pack_broadcast_to(self):
        m = Message(frm=CONTROLLER_ADDR, to=BROADCAST_ADDR, seq=1, mtype="C", payload="register")
        result = pack(m)
        self.assertTrue(result.startswith("CT2|C|*|1|C|"))

    def test_pack_ping(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=5, mtype="P", payload="")
        result = pack(m)
        self.assertEqual(result, "CT2|C|deadbeef|5|P|")


class TestUnpack(unittest.TestCase):
    def test_unpack_valid(self):
        raw = "CT2|C|deadbeef|41|C|bHM="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C")
        self.assertEqual(m.to, "deadbeef")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls")

    def test_unpack_response(self):
        raw = "CT2|deadbeef|C|41|R|cmVzdWx0YWRv"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "deadbeef")
        self.assertEqual(m.to, "C")
        self.assertEqual(m.mtype, "R")
        self.assertEqual(m.payload, "resultado")

    def test_unpack_error(self):
        raw = "CT2|deadbeef|C|41|E|ZXJyb3I="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.mtype, "E")
        self.assertEqual(m.payload, "error")

    def test_unpack_ping(self):
        raw = "CT2|C|deadbeef|5|P|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C")
        self.assertEqual(m.to, "deadbeef")
        self.assertEqual(m.seq, 5)
        self.assertEqual(m.mtype, "P")
        self.assertEqual(m.payload, "")

    def test_unpack_unicode(self):
        import base64

        payload = "cañón"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        raw = f"CT2|deadbeef|C|1|R|{encoded}"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.payload, "cañón")

    def test_unpack_rejects_legacy_ct1_signature(self):
        self.assertIsNone(unpack("CT1|C|deadbeef|1|C|bHM="))

    def test_unpack_rejects_legacy_cb1_signature(self):
        self.assertIsNone(unpack("CB1|C|deadbeef|1|C|bHM="))

    def test_unpack_rejects_bad_from_address(self):
        self.assertIsNone(unpack("CT2|X|deadbeef|1|C|bHM="))

    def test_unpack_rejects_bad_to_address(self):
        self.assertIsNone(unpack("CT2|C|Z|1|C|bHM="))

    def test_unpack_rejects_short_hex_from(self):
        self.assertIsNone(unpack("CT2|dead|deadbeef|1|C|bHM="))

    def test_unpack_rejects_uppercase_hex(self):
        self.assertIsNone(unpack("CT2|DeadBeef|C|1|C|bHM="))

    def test_unpack_invalid_prefix(self):
        self.assertIsNone(unpack("XX|C|deadbeef|1|C|bHM="))

    def test_unpack_too_few_parts(self):
        self.assertIsNone(unpack("CT2|C|deadbeef|1|C"))

    def test_unpack_too_many_parts(self):
        self.assertIsNone(unpack("CT2|C|deadbeef|1|C|bHM=|extra"))

    def test_unpack_non_integer_seq(self):
        self.assertIsNone(unpack("CT2|C|deadbeef|abc|C|bHM="))

    def test_unpack_invalid_base64(self):
        self.assertIsNone(unpack("CT2|C|deadbeef|1|C|@@@notb64@@@"))

    def test_unpack_empty_string(self):
        self.assertIsNone(unpack(""))

    def test_unpack_none(self):
        self.assertIsNone(unpack(None))

    def test_unpack_ack(self):
        raw = "CT2|deadbeef|C|41|A|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "deadbeef")
        self.assertEqual(m.to, "C")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "A")
        self.assertEqual(m.payload, "")

    def test_unpack_broadcast_to(self):
        raw = "CT2|C|*|1|C|eyJvcCI6ICJyZWdpc3RlciJ9"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.to, "*")
        self.assertEqual(m.payload, '{"op": "register"}')


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls -la")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.frm, m2.frm)
        self.assertEqual(m.to, m2.to)
        self.assertEqual(m.seq, m2.seq)
        self.assertEqual(m.mtype, m2.mtype)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_unicode(self):
        m = Message(frm=TEST_REMOTE_ID, to=CONTROLLER_ADDR, seq=99, mtype="R", payload="résultat café")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_pipes_newlines_tabs(self):
        payload = "echo 'hello|world' && cat file.txt\nnewline\ttab"
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=5, mtype="C", payload=payload)
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_empty_payload(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_error_type(self):
        m = Message(frm=TEST_REMOTE_ID, to=CONTROLLER_ADDR, seq=7, mtype="E", payload="command not found")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertEqual(m2.mtype, "E")
        self.assertEqual(m2.payload, "command not found")

    def test_roundtrip_ping(self):
        m = Message(frm=CONTROLLER_ADDR, to=TEST_REMOTE_ID, seq=3, mtype="P", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.mtype, "P")
        self.assertEqual(m2.payload, "")

    def test_roundtrip_broadcast(self):
        m = Message(frm=CONTROLLER_ADDR, to=BROADCAST_ADDR, seq=1, mtype="C", payload='{"op": "register"}')
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.to, BROADCAST_ADDR)
        self.assertEqual(m2.payload, '{"op": "register"}')


class TestValidate(unittest.TestCase):
    def test_validate_valid_for_remote(self):
        raw = "CT2|C|deadbeef|1|C|bHM="
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_valid_for_controller(self):
        raw = "CT2|deadbeef|C|1|R|bHM="
        self.assertTrue(validate(raw, CONTROLLER_ADDR))

    def test_validate_rejects_missing_prefix(self):
        raw = "XX|C|deadbeef|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_legacy_ct1_signature(self):
        raw = "CT1|C|deadbeef|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_wrong_to(self):
        # message addressed to C, but I am deadbeef
        raw = "CT2|C|C|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_from_equals_my_id(self):
        # from deadbeef, I am deadbeef — self-addressed, reject
        raw = "CT2|deadbeef|deadbeef|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_from_equals_controller_id(self):
        # from C, I am C — self-addressed, reject
        raw = "CT2|C|C|1|C|bHM="
        self.assertFalse(validate(raw, CONTROLLER_ADDR))

    def test_validate_rejects_bad_addresses(self):
        self.assertFalse(validate("CT2|X|deadbeef|1|C|bHM=", TEST_REMOTE_ID))
        self.assertFalse(validate("CT2|C|Z|1|C|bHM=", TEST_REMOTE_ID))

    def test_validate_rejects_malformed_format(self):
        self.assertFalse(validate("CT2|C|deadbeef|1|C", TEST_REMOTE_ID))
        self.assertFalse(validate("CT2|C|deadbeef|1|C|bHM=|extra", TEST_REMOTE_ID))
        self.assertFalse(validate("", TEST_REMOTE_ID))

    def test_validate_rejects_non_integer_seq(self):
        self.assertFalse(validate("CT2|C|deadbeef|abc|C|bHM=", TEST_REMOTE_ID))

    def test_validate_rejects_invalid_base64(self):
        self.assertFalse(validate("CT2|C|deadbeef|1|C|@@@bad@@@", TEST_REMOTE_ID))

    def test_validate_accepts_ack_for_controller(self):
        raw = "CT2|deadbeef|C|41|A|"
        self.assertTrue(validate(raw, CONTROLLER_ADDR))

    def test_validate_accepts_ack_for_remote(self):
        raw = "CT2|C|deadbeef|41|A|"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_ping_for_remote(self):
        raw = "CT2|C|deadbeef|5|P|"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    # ── broadcast routing ──────────────────────────────────────────────

    def test_validate_accepts_broadcast_for_remote(self):
        raw = "CT2|C|*|1|C|bHM="
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_broadcast_for_controller(self):
        # A remote could broadcast to '*' — controller should accept it
        raw = "CT2|deadbeef|*|1|R|bHM="
        self.assertTrue(validate(raw, CONTROLLER_ADDR))

    def test_validate_rejects_broadcast_from_self(self):
        # from deadbeef, to *, I am deadbeef — self-addressed, reject
        raw = "CT2|deadbeef|*|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_broadcast_from_controller_accepted_by_remote(self):
        raw = "CT2|C|*|1|C|eyJvcCI6ICJyZWdpc3RlciJ9"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_broadcast_not_accepted_by_controller_from_self(self):
        # Controller broadcasting to itself via '*' — reject
        raw = "CT2|C|*|1|C|bHM="
        self.assertFalse(validate(raw, CONTROLLER_ADDR))


class TestSeqTracker(unittest.TestCase):
    def test_should_process_new_seq(self):
        t = SeqTracker()
        self.assertTrue(t.should_process(1))

    def test_should_not_process_duplicate_seq(self):
        t = SeqTracker()
        self.assertTrue(t.should_process(1))
        t.mark_done(1, "result")
        self.assertFalse(t.should_process(1))

    def test_mark_done_and_get_cached(self):
        t = SeqTracker()
        t.mark_done(5, "my response")
        self.assertEqual(t.get_cached(5), "my response")

    def test_get_cached_no_entry(self):
        t = SeqTracker()
        self.assertIsNone(t.get_cached(99))

    def test_mark_done_overwrites(self):
        t = SeqTracker()
        t.mark_done(1, "first")
        t.mark_done(1, "second")
        self.assertEqual(t.get_cached(1), "second")

    def test_multiple_seqs(self):
        t = SeqTracker()
        t.mark_done(1, "r1")
        t.mark_done(2, "r2")
        t.mark_done(3, "r3")
        self.assertEqual(t.get_cached(1), "r1")
        self.assertEqual(t.get_cached(2), "r2")
        self.assertEqual(t.get_cached(3), "r3")

    def test_should_process_different_seqs(self):
        t = SeqTracker()
        self.assertTrue(t.should_process(1))
        t.mark_done(1, "r1")
        # a different seq should still be processable
        self.assertTrue(t.should_process(2))
        t.mark_done(2, "r2")
        # seq 1 is still cached
        self.assertFalse(t.should_process(1))
        self.assertFalse(t.should_process(2))
        self.assertTrue(t.should_process(3))

    # ── async state tests ─────────────────────────────────────────────

    def test_get_state_new(self):
        t = SeqTracker()
        self.assertEqual(t.get_state(1), "new")

    def test_mark_processing_sets_state(self):
        t = SeqTracker()
        t.mark_processing(10)
        self.assertEqual(t.get_state(10), "processing")

    def test_mark_done_sets_state(self):
        t = SeqTracker()
        t.mark_done(10, "result")
        self.assertEqual(t.get_state(10), "done")

    def test_should_process_false_when_processing(self):
        t = SeqTracker()
        t.mark_processing(5)
        self.assertFalse(t.should_process(5))

    def test_should_process_false_when_done(self):
        t = SeqTracker()
        t.mark_done(5, "result")
        self.assertFalse(t.should_process(5))

    def test_get_cached_returns_response_for_done(self):
        t = SeqTracker()
        t.mark_done(7, "cached response")
        self.assertEqual(t.get_cached(7), "cached response")

    def test_get_cached_none_when_processing(self):
        t = SeqTracker()
        t.mark_processing(7)
        self.assertIsNone(t.get_cached(7))

    def test_get_cached_none_when_new(self):
        t = SeqTracker()
        self.assertIsNone(t.get_cached(99))

    def test_transition_processing_to_done(self):
        t = SeqTracker()
        t.mark_processing(3)
        self.assertEqual(t.get_state(3), "processing")
        self.assertIsNone(t.get_cached(3))
        t.mark_done(3, "final result")
        self.assertEqual(t.get_state(3), "done")
        self.assertEqual(t.get_cached(3), "final result")

    def test_last_seq_returns_max(self):
        t = SeqTracker()
        t.mark_processing(1)
        t.mark_done(5, "r5")
        t.mark_processing(3)
        self.assertEqual(t.last_seq, 5)

    def test_last_seq_none_when_empty(self):
        t = SeqTracker()
        self.assertIsNone(t.last_seq)

    # ── typed response cache (R/E discriminator) ──────────────────────

    def test_mark_done_defaults_to_response_not_error(self):
        t = SeqTracker()
        t.mark_done(3, "ok")
        self.assertEqual(t.get_cached_response(3), ("ok", False))

    def test_duplicate_cached_error_stays_error(self):
        t = SeqTracker()
        t.mark_done(2, "boom", is_error=True)
        self.assertEqual(t.get_cached_response(2), ("boom", True))
        # duplicate delivery of the same seq must keep its E-ness
        self.assertEqual(t.get_cached_response(2), ("boom", True))
        self.assertEqual(t.get_cached(2), "boom")

    def test_cached_response_types_are_independent_per_seq(self):
        t = SeqTracker()
        t.mark_done(1, "ok", is_error=False)
        t.mark_done(2, "boom", is_error=True)
        self.assertEqual(t.get_cached_response(1), ("ok", False))
        self.assertEqual(t.get_cached_response(2), ("boom", True))

    def test_get_cached_response_none_when_processing(self):
        t = SeqTracker()
        t.mark_processing(7)
        self.assertIsNone(t.get_cached_response(7))

    def test_get_cached_response_none_when_new(self):
        t = SeqTracker()
        self.assertIsNone(t.get_cached_response(99))


if __name__ == "__main__":
    unittest.main()