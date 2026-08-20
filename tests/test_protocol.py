"""Unit tests for cliptunnel_mcp.protocol — stdlib unittest, zero deps."""
from __future__ import annotations

import unittest

import cliptunnel_mcp
from cliptunnel_mcp.protocol import (
    PROTOCOL_SIG,
    Message,
    MsgType,
    Role,
    SeqTracker,
    pack,
    unpack,
    validate,
)


class TestPackageIdentity(unittest.TestCase):
    def test_version(self):
        from importlib.metadata import version

        self.assertEqual(cliptunnel_mcp.__version__, version("cliptunnel-mcp"))

    def test_reexports_protocol_surface(self):
        self.assertIs(cliptunnel_mcp.pack, pack)
        self.assertIs(cliptunnel_mcp.unpack, unpack)
        self.assertIs(cliptunnel_mcp.validate, validate)
        self.assertIs(cliptunnel_mcp.Message, Message)
        self.assertIs(cliptunnel_mcp.Role, Role)
        self.assertIs(cliptunnel_mcp.MsgType, MsgType)
        self.assertIs(cliptunnel_mcp.SeqTracker, SeqTracker)


class TestMessageConstruction(unittest.TestCase):
    def test_message_fields(self):
        m = Message(frm="C", to="A", seq=1, mtype="C", payload="ls -la")
        self.assertEqual(m.frm, "C")
        self.assertEqual(m.to, "A")
        self.assertEqual(m.seq, 1)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls -la")

    def test_role_enum_values(self):
        self.assertEqual(Role.CONTROLLER.value, "C")
        self.assertEqual(Role.AGENT.value, "A")

    def test_msgtype_enum_values(self):
        self.assertEqual(MsgType.COMMAND.value, "C")
        self.assertEqual(MsgType.RESPONSE.value, "R")
        self.assertEqual(MsgType.ERROR.value, "E")
        self.assertEqual(MsgType.ACK.value, "A")

    def test_protocol_sig(self):
        self.assertEqual(PROTOCOL_SIG, "CT1")


class TestPack(unittest.TestCase):
    def test_pack_basic(self):
        m = Message(frm="C", to="A", seq=41, mtype="C", payload="ls")
        result = pack(m)
        self.assertTrue(result.startswith("CT1|C|A|41|C|"))
        # payload "ls" base64 = "bHM="
        self.assertEqual(result, "CT1|C|A|41|C|bHM=")

    def test_pack_unicode(self):
        m = Message(frm="A", to="C", seq=1, mtype="R", payload="cañón")
        result = pack(m)
        self.assertTrue(result.startswith("CT1|A|C|1|R|"))
        # payload must be base64-encoded (no raw unicode in wire format)
        self.assertNotIn("cañón", result)

    def test_pack_special_chars(self):
        m = Message(frm="C", to="A", seq=1, mtype="C", payload="echo 'a|b|c'")
        result = pack(m)
        # pipe chars in payload must not appear raw (they're base64-encoded)
        parts = result.split("|")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "CT1")

    def test_pack_empty_payload(self):
        m = Message(frm="C", to="A", seq=1, mtype="C", payload="")
        self.assertEqual(pack(m), "CT1|C|A|1|C|")

    def test_pack_ack_empty_payload(self):
        m = Message(frm="A", to="C", seq=41, mtype="A", payload="")
        result = pack(m)
        self.assertTrue(result.startswith("CT1|A|C|41|A|"))
        # empty payload base64 = ""
        self.assertEqual(result, "CT1|A|C|41|A|")


class TestUnpack(unittest.TestCase):
    def test_unpack_valid(self):
        raw = "CT1|C|A|41|C|bHM="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C")
        self.assertEqual(m.to, "A")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls")

    def test_unpack_response(self):
        raw = "CT1|A|C|41|R|cmVzdWx0YWRv"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "A")
        self.assertEqual(m.to, "C")
        self.assertEqual(m.mtype, "R")
        self.assertEqual(m.payload, "resultado")

    def test_unpack_error(self):
        raw = "CT1|A|C|41|E|ZXJyb3I="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.mtype, "E")
        self.assertEqual(m.payload, "error")

    def test_unpack_unicode(self):
        import base64

        payload = "cañón"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        raw = f"CT1|A|C|1|R|{encoded}"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.payload, "cañón")

    def test_unpack_rejects_legacy_cb1_signature(self):
        self.assertIsNone(unpack("CB1|C|A|1|C|bHM="))

    def test_unpack_rejects_bad_from_role(self):
        self.assertIsNone(unpack("CT1|X|A|1|C|bHM="))

    def test_unpack_rejects_bad_to_role(self):
        self.assertIsNone(unpack("CT1|C|Z|1|C|bHM="))

    def test_unpack_rejects_legacy_host_vdi_roles(self):
        self.assertIsNone(unpack("CT1|H|V|1|C|bHM="))

    def test_unpack_invalid_prefix(self):
        self.assertIsNone(unpack("XX|C|A|1|C|bHM="))

    def test_unpack_too_few_parts(self):
        self.assertIsNone(unpack("CT1|C|A|1|C"))

    def test_unpack_too_many_parts(self):
        self.assertIsNone(unpack("CT1|C|A|1|C|bHM=|extra"))

    def test_unpack_non_integer_seq(self):
        self.assertIsNone(unpack("CT1|C|A|abc|C|bHM="))

    def test_unpack_invalid_base64(self):
        self.assertIsNone(unpack("CT1|C|A|1|C|@@@notb64@@@"))

    def test_unpack_empty_string(self):
        self.assertIsNone(unpack(""))

    def test_unpack_none(self):
        self.assertIsNone(unpack(None))

    def test_unpack_ack(self):
        raw = "CT1|A|C|41|A|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "A")
        self.assertEqual(m.to, "C")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "A")
        self.assertEqual(m.payload, "")


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        m = Message(frm="C", to="A", seq=1, mtype="C", payload="ls -la")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.frm, m2.frm)
        self.assertEqual(m.to, m2.to)
        self.assertEqual(m.seq, m2.seq)
        self.assertEqual(m.mtype, m2.mtype)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_unicode(self):
        m = Message(frm="A", to="C", seq=99, mtype="R", payload="résultat café")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_pipes_newlines_tabs(self):
        payload = "echo 'hello|world' && cat file.txt\nnewline\ttab"
        m = Message(frm="C", to="A", seq=5, mtype="C", payload=payload)
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_empty_payload(self):
        m = Message(frm="C", to="A", seq=1, mtype="C", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_error_type(self):
        m = Message(frm="A", to="C", seq=7, mtype="E", payload="command not found")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertEqual(m2.mtype, "E")
        self.assertEqual(m2.payload, "command not found")


class TestValidate(unittest.TestCase):
    def test_validate_valid_for_agent(self):
        raw = "CT1|C|A|1|C|bHM="
        self.assertTrue(validate(raw, Role.AGENT))

    def test_validate_valid_for_controller(self):
        raw = "CT1|A|C|1|R|bHM="
        self.assertTrue(validate(raw, Role.CONTROLLER))

    def test_validate_rejects_missing_prefix(self):
        raw = "XX|C|A|1|C|bHM="
        self.assertFalse(validate(raw, Role.AGENT))

    def test_validate_rejects_legacy_cb1_signature(self):
        raw = "CB1|C|A|1|C|bHM="
        self.assertFalse(validate(raw, Role.AGENT))

    def test_validate_rejects_wrong_to(self):
        # message addressed to C, but I am A
        raw = "CT1|C|C|1|C|bHM="
        self.assertFalse(validate(raw, Role.AGENT))

    def test_validate_rejects_from_equals_own_role(self):
        # from A, I am A — self-addressed, reject
        raw = "CT1|A|A|1|C|bHM="
        self.assertFalse(validate(raw, Role.AGENT))

    def test_validate_rejects_bad_roles(self):
        self.assertFalse(validate("CT1|X|A|1|C|bHM=", Role.AGENT))
        self.assertFalse(validate("CT1|C|Z|1|C|bHM=", Role.AGENT))

    def test_validate_rejects_malformed_format(self):
        self.assertFalse(validate("CT1|C|A|1|C", Role.AGENT))
        self.assertFalse(validate("CT1|C|A|1|C|bHM=|extra", Role.AGENT))
        self.assertFalse(validate("", Role.AGENT))

    def test_validate_rejects_non_integer_seq(self):
        self.assertFalse(validate("CT1|C|A|abc|C|bHM=", Role.AGENT))

    def test_validate_rejects_invalid_base64(self):
        self.assertFalse(validate("CT1|C|A|1|C|@@@bad@@@", Role.AGENT))

    def test_validate_accepts_string_role(self):
        raw = "CT1|C|A|1|C|bHM="
        self.assertTrue(validate(raw, "A"))
        self.assertFalse(validate(raw, "C"))

    def test_validate_accepts_ack_for_controller(self):
        raw = "CT1|A|C|41|A|"
        self.assertTrue(validate(raw, Role.CONTROLLER))

    def test_validate_accepts_ack_for_agent(self):
        raw = "CT1|C|A|41|A|"
        self.assertTrue(validate(raw, Role.AGENT))


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
