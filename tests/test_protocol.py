"""Unit tests for cliptunnel_mcp.protocol — stdlib unittest, zero deps."""
from __future__ import annotations

import base64
import unittest

import cliptunnel_mcp
from cliptunnel_mcp.protocol import (
    BROADCAST_ADDR,
    CONTROLLER_ADDR,
    PROTOCOL_SIG,
    PROTOCOL_SIG_ENC,
    PROTOCOL_VERSION,
    Message,
    MsgType,
    SeqTracker,
    generate_controller_id,
    generate_remote_id,
    is_broadcast,
    is_controller,
    is_encrypted,
    is_remote,
    is_valid_address,
    is_valid_from_address,
    is_valid_to_address,
    pack,
    unpack,
    validate,
)

# Fixed test IDs — CT3 format: R/C + 7 hex.
TEST_REMOTE_ID = "R1a2b3c4"
TEST_CONTROLLER_ID = "C1a2b3c4"


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
        self.assertIs(cliptunnel_mcp.generate_controller_id, generate_controller_id)
        self.assertIs(cliptunnel_mcp.CONTROLLER_ADDR, CONTROLLER_ADDR)
        self.assertIs(cliptunnel_mcp.BROADCAST_ADDR, BROADCAST_ADDR)


class TestConstants(unittest.TestCase):
    def test_controller_addr(self):
        self.assertEqual(CONTROLLER_ADDR, "C")

    def test_broadcast_addr(self):
        self.assertEqual(BROADCAST_ADDR, "*")

    def test_protocol_sig(self):
        self.assertEqual(PROTOCOL_SIG, "CT3")

    def test_protocol_version(self):
        self.assertEqual(PROTOCOL_VERSION, 3)


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

    def test_announce(self):
        self.assertEqual(MsgType.ANNOUNCE.value, "N")


class TestMessageConstruction(unittest.TestCase):
    def test_message_fields(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls -la")
        self.assertEqual(m.frm, TEST_CONTROLLER_ID)
        self.assertEqual(m.to, TEST_REMOTE_ID)
        self.assertEqual(m.seq, 1)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls -la")

    def test_message_fields_are_plain_strings(self):
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=99, mtype="R", payload="ok")
        self.assertIsInstance(m.frm, str)
        self.assertIsInstance(m.to, str)


class TestGenerateRemoteId(unittest.TestCase):
    def test_generates_r_prefix_8_chars(self):
        rid = generate_remote_id()
        self.assertEqual(len(rid), 8)
        self.assertTrue(rid.startswith("R"))
        self.assertTrue(is_valid_address(rid))

    def test_generates_different_ids(self):
        ids = {generate_remote_id() for _ in range(100)}
        # Collisions in 100 random IDs are astronomically unlikely.
        self.assertGreater(len(ids), 90)

    def test_generated_id_is_r_plus_7_hex(self):
        rid = generate_remote_id()
        self.assertEqual(rid[0], "R")
        self.assertTrue(all(c in "0123456789abcdef" for c in rid[1:]))


class TestGenerateControllerId(unittest.TestCase):
    def test_generates_c_prefix_8_chars(self):
        cid = generate_controller_id()
        self.assertEqual(len(cid), 8)
        self.assertTrue(cid.startswith("C"))
        self.assertTrue(is_valid_address(cid))

    def test_generates_different_ids(self):
        ids = {generate_controller_id() for _ in range(100)}
        self.assertGreater(len(ids), 90)

    def test_generated_id_is_c_plus_7_hex(self):
        cid = generate_controller_id()
        self.assertEqual(cid[0], "C")
        self.assertTrue(all(c in "0123456789abcdef" for c in cid[1:]))

    def test_generated_controller_id_is_controller(self):
        cid = generate_controller_id()
        self.assertTrue(is_controller(cid))
        self.assertFalse(is_remote(cid))


class TestIsValidAddress(unittest.TestCase):
    def test_controller_addr_valid(self):
        self.assertTrue(is_valid_address(CONTROLLER_ADDR))

    def test_broadcast_addr_valid(self):
        self.assertTrue(is_valid_address(BROADCAST_ADDR))

    def test_ct3_controller_id_valid(self):
        self.assertTrue(is_valid_address(TEST_CONTROLLER_ID))
        self.assertTrue(is_valid_address("C0000000"))
        self.assertTrue(is_valid_address("Cabcdef0"))

    def test_ct3_remote_id_valid(self):
        self.assertTrue(is_valid_address(TEST_REMOTE_ID))
        self.assertTrue(is_valid_address("R0000000"))
        self.assertTrue(is_valid_address("Rabcdef0"))

    def test_legacy_8_char_hex_valid(self):
        # Legacy bare 8-hex IDs are still valid for backward compat.
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

    def test_short_ct3_id_invalid(self):
        self.assertFalse(is_valid_address("C1a2b3c"))
        self.assertFalse(is_valid_address("R1a2b3c"))


class TestIsValidFromAddress(unittest.TestCase):
    def test_controller_valid(self):
        self.assertTrue(is_valid_from_address(CONTROLLER_ADDR))

    def test_ct3_controller_id_valid(self):
        self.assertTrue(is_valid_from_address(TEST_CONTROLLER_ID))

    def test_ct3_remote_id_valid(self):
        self.assertTrue(is_valid_from_address(TEST_REMOTE_ID))

    def test_legacy_hex_valid(self):
        self.assertTrue(is_valid_from_address("deadbeef"))

    def test_broadcast_invalid(self):
        self.assertFalse(is_valid_from_address(BROADCAST_ADDR))

    def test_short_hex_invalid(self):
        self.assertFalse(is_valid_from_address("dead"))


class TestIsValidToAddress(unittest.TestCase):
    def test_controller_valid(self):
        self.assertTrue(is_valid_to_address(CONTROLLER_ADDR))

    def test_ct3_controller_id_valid(self):
        self.assertTrue(is_valid_to_address(TEST_CONTROLLER_ID))

    def test_ct3_remote_id_valid(self):
        self.assertTrue(is_valid_to_address(TEST_REMOTE_ID))

    def test_broadcast_valid(self):
        self.assertTrue(is_valid_to_address(BROADCAST_ADDR))

    def test_legacy_hex_valid(self):
        self.assertTrue(is_valid_to_address("deadbeef"))

    def test_short_hex_invalid(self):
        self.assertFalse(is_valid_to_address("dead"))


class TestUnpackRejectsBroadcastAsFrom(unittest.TestCase):
    def test_broadcast_as_from_is_rejected(self):
        wire = pack(Message(
            frm=BROADCAST_ADDR, to=CONTROLLER_ADDR,
            seq=1, mtype=MsgType.COMMAND.value, payload="test",
        ))
        # pack() will produce the wire string, but unpack() must reject it
        # because '*' is not a valid from-address.
        self.assertIsNone(unpack(wire))


class TestIsValidAddressEmpty(unittest.TestCase):
    def test_empty_invalid(self):
        self.assertFalse(is_valid_address(""))


class TestIsController(unittest.TestCase):
    def test_controller_addr(self):
        self.assertTrue(is_controller(CONTROLLER_ADDR))
        self.assertTrue(is_controller("C"))

    def test_ct3_controller_id(self):
        self.assertTrue(is_controller(TEST_CONTROLLER_ID))
        self.assertTrue(is_controller("C0000000"))
        self.assertTrue(is_controller("Cabcdef0"))

    def test_non_controller(self):
        self.assertFalse(is_controller(TEST_REMOTE_ID))
        self.assertFalse(is_controller(BROADCAST_ADDR))
        self.assertFalse(is_controller("deadbeef"))


class TestIsRemote(unittest.TestCase):
    def test_ct3_remote_id(self):
        self.assertTrue(is_remote(TEST_REMOTE_ID))
        self.assertTrue(is_remote("R0000000"))
        self.assertTrue(is_remote("Rabcdef0"))

    def test_non_remote(self):
        self.assertFalse(is_remote(TEST_CONTROLLER_ID))
        self.assertFalse(is_remote(BROADCAST_ADDR))
        self.assertFalse(is_remote("deadbeef"))


class TestIsBroadcast(unittest.TestCase):
    def test_broadcast_addr(self):
        self.assertTrue(is_broadcast(BROADCAST_ADDR))
        self.assertTrue(is_broadcast("*"))

    def test_non_broadcast(self):
        self.assertFalse(is_broadcast(CONTROLLER_ADDR))
        self.assertFalse(is_broadcast(TEST_REMOTE_ID))


class TestPack(unittest.TestCase):
    def test_pack_basic(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=41, mtype="C", payload="ls")
        result = pack(m)
        self.assertTrue(result.startswith("CT3|C1a2b3c4|R1a2b3c4|41|C|"))
        # payload "ls" base64 = "bHM="
        self.assertEqual(result, "CT3|C1a2b3c4|R1a2b3c4|41|C|bHM=")

    def test_pack_unicode(self):
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=1, mtype="R", payload="cañón")
        result = pack(m)
        self.assertTrue(result.startswith("CT3|R1a2b3c4|C1a2b3c4|1|R|"))
        # payload must be base64-encoded (no raw unicode in wire format)
        self.assertNotIn("cañón", result)

    def test_pack_special_chars(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="echo 'a|b|c'")
        result = pack(m)
        # pipe chars in payload must not appear raw (they're base64-encoded)
        parts = result.split("|")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "CT3")

    def test_pack_empty_payload(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="")
        self.assertEqual(pack(m), "CT3|C1a2b3c4|R1a2b3c4|1|C|")

    def test_pack_ack_empty_payload(self):
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=41, mtype="A", payload="")
        result = pack(m)
        self.assertTrue(result.startswith("CT3|R1a2b3c4|C1a2b3c4|41|A|"))
        # empty payload base64 = ""
        self.assertEqual(result, "CT3|R1a2b3c4|C1a2b3c4|41|A|")

    def test_pack_broadcast_to(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=BROADCAST_ADDR, seq=1, mtype="C", payload="register")
        result = pack(m)
        self.assertTrue(result.startswith("CT3|C1a2b3c4|*|1|C|"))

    def test_pack_ping(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=5, mtype="P", payload="")
        result = pack(m)
        self.assertEqual(result, "CT3|C1a2b3c4|R1a2b3c4|5|P|")

    def test_pack_announce(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=BROADCAST_ADDR, seq=1, mtype="N", payload="")
        result = pack(m)
        self.assertEqual(result, "CT3|C1a2b3c4|*|1|N|")


class TestUnpack(unittest.TestCase):
    def test_unpack_valid(self):
        raw = "CT3|C1a2b3c4|R1a2b3c4|41|C|bHM="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C1a2b3c4")
        self.assertEqual(m.to, "R1a2b3c4")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "C")
        self.assertEqual(m.payload, "ls")

    def test_unpack_response(self):
        raw = "CT3|R1a2b3c4|C1a2b3c4|41|R|cmVzdWx0YWRv"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "R1a2b3c4")
        self.assertEqual(m.to, "C1a2b3c4")
        self.assertEqual(m.mtype, "R")
        self.assertEqual(m.payload, "resultado")

    def test_unpack_error(self):
        raw = "CT3|R1a2b3c4|C1a2b3c4|41|E|ZXJyb3I="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.mtype, "E")
        self.assertEqual(m.payload, "error")

    def test_unpack_ping(self):
        raw = "CT3|C1a2b3c4|R1a2b3c4|5|P|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C1a2b3c4")
        self.assertEqual(m.to, "R1a2b3c4")
        self.assertEqual(m.seq, 5)
        self.assertEqual(m.mtype, "P")
        self.assertEqual(m.payload, "")

    def test_unpack_announce(self):
        raw = "CT3|C1a2b3c4|*|1|N|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "C1a2b3c4")
        self.assertEqual(m.to, "*")
        self.assertEqual(m.seq, 1)
        self.assertEqual(m.mtype, "N")
        self.assertEqual(m.payload, "")

    def test_unpack_unicode(self):

        payload = "cañón"
        encoded = base64.b64encode(payload.encode("utf-8")).decode("ascii")
        raw = f"CT3|R1a2b3c4|C1a2b3c4|1|R|{encoded}"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.payload, "cañón")

    def test_unpack_accepts_legacy_ct2_format(self):
        # Legacy bare 8-hex IDs are accepted for backward compat.
        raw = "CT3|deadbeef|C1a2b3c4|1|C|bHM="
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "deadbeef")
        self.assertEqual(m.to, "C1a2b3c4")

    def test_unpack_rejects_legacy_ct1_signature(self):
        self.assertIsNone(unpack("CT1|C1a2b3c4|R1a2b3c4|1|C|bHM="))

    def test_unpack_rejects_legacy_cb1_signature(self):
        self.assertIsNone(unpack("CB1|C1a2b3c4|R1a2b3c4|1|C|bHM="))

    def test_unpack_rejects_ct2_signature(self):
        self.assertIsNone(unpack("CT2|C1a2b3c4|R1a2b3c4|1|C|bHM="))

    def test_unpack_rejects_bad_from_address(self):
        self.assertIsNone(unpack("CT3|X|deadbeef|1|C|bHM="))

    def test_unpack_rejects_bad_to_address(self):
        self.assertIsNone(unpack("CT3|C1a2b3c4|Z|1|C|bHM="))

    def test_unpack_rejects_short_hex_from(self):
        self.assertIsNone(unpack("CT3|dead|deadbeef|1|C|bHM="))

    def test_unpack_rejects_uppercase_hex(self):
        self.assertIsNone(unpack("CT3|DeadBeef|C1a2b3c4|1|C|bHM="))

    def test_unpack_invalid_prefix(self):
        self.assertIsNone(unpack("XX|C1a2b3c4|R1a2b3c4|1|C|bHM="))

    def test_unpack_too_few_parts(self):
        self.assertIsNone(unpack("CT3|C1a2b3c4|R1a2b3c4|1|C"))

    def test_unpack_too_many_parts(self):
        self.assertIsNone(unpack("CT3|C1a2b3c4|R1a2b3c4|1|C|bHM=|extra"))

    def test_unpack_non_integer_seq(self):
        self.assertIsNone(unpack("CT3|C1a2b3c4|R1a2b3c4|abc|C|bHM="))

    def test_unpack_invalid_base64(self):
        self.assertIsNone(unpack("CT3|C1a2b3c4|R1a2b3c4|1|C|@@@notb64@@@"))

    def test_unpack_empty_string(self):
        self.assertIsNone(unpack(""))

    def test_unpack_none(self):
        self.assertIsNone(unpack(None))

    def test_unpack_ack(self):
        raw = "CT3|R1a2b3c4|C1a2b3c4|41|A|"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.frm, "R1a2b3c4")
        self.assertEqual(m.to, "C1a2b3c4")
        self.assertEqual(m.seq, 41)
        self.assertEqual(m.mtype, "A")
        self.assertEqual(m.payload, "")

    def test_unpack_broadcast_to(self):
        raw = "CT3|C1a2b3c4|*|1|C|eyJvcCI6ICJyZWdpc3RlciJ9"
        m = unpack(raw)
        self.assertIsNotNone(m)
        self.assertEqual(m.to, "*")
        self.assertEqual(m.payload, '{"op": "register"}')


class TestRoundTrip(unittest.TestCase):
    def test_roundtrip_basic(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls -la")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.frm, m2.frm)
        self.assertEqual(m.to, m2.to)
        self.assertEqual(m.seq, m2.seq)
        self.assertEqual(m.mtype, m2.mtype)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_unicode(self):
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=99, mtype="R", payload="résultat café")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_pipes_newlines_tabs(self):
        payload = "echo 'hello|world' && cat file.txt\nnewline\ttab"
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=5, mtype="C", payload=payload)
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_empty_payload(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m.payload, m2.payload)

    def test_roundtrip_error_type(self):
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=7, mtype="E", payload="command not found")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertEqual(m2.mtype, "E")
        self.assertEqual(m2.payload, "command not found")

    def test_roundtrip_ping(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=3, mtype="P", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.mtype, "P")
        self.assertEqual(m2.payload, "")

    def test_roundtrip_announce(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=BROADCAST_ADDR, seq=1, mtype="N", payload="")
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.mtype, "N")
        self.assertEqual(m2.payload, "")

    def test_roundtrip_broadcast(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=BROADCAST_ADDR, seq=1, mtype="C", payload='{"op": "register"}')
        raw = pack(m)
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.to, BROADCAST_ADDR)
        self.assertEqual(m2.payload, '{"op": "register"}')


class TestEncryptedPack(unittest.TestCase):
    def test_pack_encrypted_uses_ct3e_prefix(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")  # 32 bytes of '0'
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls")
        result = pack(m, aes_key=key)
        self.assertTrue(result.startswith("CT3E|C1a2b3c4|R1a2b3c4|1|C|"))
        # The payload field is encrypted, not base64("ls")
        self.assertNotIn("bHM=", result)

    def test_pack_encrypted_header_is_plaintext(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=42, mtype="C", payload="secret")
        result = pack(m, aes_key=key)
        parts = result.split("|")
        self.assertEqual(len(parts), 6)
        self.assertEqual(parts[0], "CT3E")
        self.assertEqual(parts[1], "C1a2b3c4")
        self.assertEqual(parts[2], "R1a2b3c4")
        self.assertEqual(parts[3], "42")
        self.assertEqual(parts[4], "C")
        # Payload is encrypted — not "secret" in plaintext
        self.assertNotIn("secret", parts[5])

    def test_pack_without_key_uses_ct3_prefix(self):
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls")
        result = pack(m)
        self.assertTrue(result.startswith("CT3|"))


class TestEncryptedUnpack(unittest.TestCase):
    def test_roundtrip_encrypted(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls -la")
        raw = pack(m, aes_key=key)
        m2 = unpack(raw, aes_key=key)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.frm, "C1a2b3c4")
        self.assertEqual(m2.to, "R1a2b3c4")
        self.assertEqual(m2.seq, 1)
        self.assertEqual(m2.mtype, "C")
        self.assertEqual(m2.payload, "ls -la")

    def test_roundtrip_encrypted_unicode(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=99, mtype="R", payload="café — 日本語")
        raw = pack(m, aes_key=key)
        m2 = unpack(raw, aes_key=key)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.payload, "café — 日本語")

    def test_roundtrip_encrypted_empty_payload(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="A", payload="")
        raw = pack(m, aes_key=key)
        m2 = unpack(raw, aes_key=key)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.payload, "")

    def test_unpack_ct3e_without_key_returns_raw_ciphertext(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="secret")
        raw = pack(m, aes_key=key)
        # Without key, should return the ciphertext as payload
        m2 = unpack(raw)
        self.assertIsNotNone(m2)
        self.assertEqual(m2.frm, "C1a2b3c4")
        # Payload is the raw base64 ciphertext, not "secret"
        self.assertNotEqual(m2.payload, "secret")

    def test_unpack_ct3e_with_wrong_key_returns_none(self):
        key1 = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        key2 = base64.b64decode("rLk1TQ06bMQQM10BkqP/qEx7rHcJUp5SzkqXhaovGyA=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="secret")
        raw = pack(m, aes_key=key1)
        m2 = unpack(raw, aes_key=key2)
        self.assertIsNone(m2)

    def test_unpack_rejects_ct3e_bad_from(self):
        raw = "CT3E|X|deadbeef|1|C|dGVzdA=="
        self.assertIsNone(unpack(raw, aes_key=base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")))

    def test_unpack_rejects_ct3e_bad_to(self):
        raw = "CT3E|C1a2b3c4|Z|1|C|dGVzdA=="
        self.assertIsNone(unpack(raw, aes_key=base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")))

    def test_unpack_rejects_ct3e_non_integer_seq(self):
        raw = "CT3E|C1a2b3c4|R1a2b3c4|abc|C|dGVzdA=="
        self.assertIsNone(unpack(raw, aes_key=base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")))


class TestIsEncrypted(unittest.TestCase):
    def test_ct3e_is_encrypted(self):
        self.assertTrue(is_encrypted("CT3E|C1a2b3c4|R1a2b3c4|1|C|dGVzdA=="))

    def test_ct3_is_not_encrypted(self):
        self.assertFalse(is_encrypted("CT3|C1a2b3c4|R1a2b3c4|1|C|dGVzdA=="))

    def test_empty_is_not_encrypted(self):
        self.assertFalse(is_encrypted(""))

    def test_other_prefix_is_not_encrypted(self):
        self.assertFalse(is_encrypted("CT3P|something"))


class TestValidateEncrypted(unittest.TestCase):
    def test_validate_accepts_ct3e_for_remote(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls")
        raw = pack(m, aes_key=key)
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_ct3e_for_controller(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=1, mtype="R", payload="result")
        raw = pack(m, aes_key=key)
        self.assertTrue(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_rejects_ct3e_self_addressed(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_REMOTE_ID, to=TEST_REMOTE_ID, seq=1, mtype="C", payload="ls")
        raw = pack(m, aes_key=key)
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_ct3e_wrong_to(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_REMOTE_ID, to=TEST_CONTROLLER_ID, seq=1, mtype="R", payload="result")
        raw = pack(m, aes_key=key)
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_ct3e_broadcast(self):
        key = base64.b64decode("tqKJ7BPAT/tt0smOh+UPRub5/nvEi3pvj3IeAFt5KdU=")
        m = Message(frm=TEST_CONTROLLER_ID, to=BROADCAST_ADDR, seq=1, mtype="N", payload="")
        raw = pack(m, aes_key=key)
        self.assertTrue(validate(raw, TEST_REMOTE_ID))


class TestValidate(unittest.TestCase):
    def test_validate_valid_for_remote(self):
        raw = "CT3|C1a2b3c4|R1a2b3c4|1|C|bHM="
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_valid_for_controller(self):
        raw = "CT3|R1a2b3c4|C1a2b3c4|1|R|bHM="
        self.assertTrue(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_rejects_missing_prefix(self):
        raw = "XX|C1a2b3c4|R1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_legacy_ct1_signature(self):
        raw = "CT1|C1a2b3c4|R1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_ct2_signature(self):
        raw = "CT2|C1a2b3c4|R1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_wrong_to(self):
        # message addressed to C1a2b3c4, but I am R1a2b3c4
        raw = "CT3|R1a2b3c4|C1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_from_equals_my_id(self):
        # from R1a2b3c4, I am R1a2b3c4 — self-addressed, reject
        raw = "CT3|R1a2b3c4|R1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_rejects_from_equals_controller_id(self):
        # from C1a2b3c4, I am C1a2b3c4 — self-addressed, reject
        raw = "CT3|C1a2b3c4|C1a2b3c4|1|C|bHM="
        self.assertFalse(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_rejects_bad_addresses(self):
        self.assertFalse(validate("CT3|X|deadbeef|1|C|bHM=", TEST_REMOTE_ID))
        self.assertFalse(validate("CT3|C1a2b3c4|Z|1|C|bHM=", TEST_REMOTE_ID))

    def test_validate_rejects_malformed_format(self):
        self.assertFalse(validate("CT3|C1a2b3c4|R1a2b3c4|1|C", TEST_REMOTE_ID))
        self.assertFalse(validate("CT3|C1a2b3c4|R1a2b3c4|1|C|bHM=|extra", TEST_REMOTE_ID))
        self.assertFalse(validate("", TEST_REMOTE_ID))

    def test_validate_rejects_non_integer_seq(self):
        self.assertFalse(validate("CT3|C1a2b3c4|R1a2b3c4|abc|C|bHM=", TEST_REMOTE_ID))

    def test_validate_rejects_invalid_base64(self):
        self.assertFalse(validate("CT3|C1a2b3c4|R1a2b3c4|1|C|@@@bad@@@", TEST_REMOTE_ID))

    def test_validate_accepts_ack_for_controller(self):
        raw = "CT3|R1a2b3c4|C1a2b3c4|41|A|"
        self.assertTrue(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_accepts_ack_for_remote(self):
        raw = "CT3|C1a2b3c4|R1a2b3c4|41|A|"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_ping_for_remote(self):
        raw = "CT3|C1a2b3c4|R1a2b3c4|5|P|"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    # ── broadcast routing ──────────────────────────────────────────────

    def test_validate_accepts_broadcast_for_remote(self):
        raw = "CT3|C1a2b3c4|*|1|C|bHM="
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_accepts_broadcast_for_controller(self):
        # A remote could broadcast to '*' — controller should accept it
        raw = "CT3|R1a2b3c4|*|1|R|bHM="
        self.assertTrue(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_rejects_broadcast_from_self(self):
        # from R1a2b3c4, to *, I am R1a2b3c4 — self-addressed, reject
        raw = "CT3|R1a2b3c4|*|1|C|bHM="
        self.assertFalse(validate(raw, TEST_REMOTE_ID))

    def test_validate_broadcast_from_controller_accepted_by_remote(self):
        raw = "CT3|C1a2b3c4|*|1|C|eyJvcCI6ICJyZWdpc3RlciJ9"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))

    def test_validate_broadcast_not_accepted_by_controller_from_self(self):
        # Controller broadcasting to itself via '*' — reject
        raw = "CT3|C1a2b3c4|*|1|C|bHM="
        self.assertFalse(validate(raw, TEST_CONTROLLER_ID))

    def test_validate_accepts_announce_for_remote(self):
        raw = "CT3|C1a2b3c4|*|1|N|"
        self.assertTrue(validate(raw, TEST_REMOTE_ID))


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