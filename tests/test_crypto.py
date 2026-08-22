"""Tests for the optional AES-256-GCM crypto layer (src/cliptunnel_mcp/crypto.py).

TDD order: these tests were written before crypto.py existed (RED), then
crypto.py was implemented to make them pass (GREEN), then edge cases were
added (TRIANGULATE).
"""

from __future__ import annotations

import base64
import os
import unittest

from cliptunnel_mcp.crypto import decrypt, encrypt, parse_key


def _random_key() -> bytes:
    """Return 32 random bytes suitable for AES-256."""
    return os.urandom(32)


class TestEncryptDecryptRoundTrip(unittest.TestCase):
    """GREEN: encrypt then decrypt recovers the original plaintext."""

    def test_round_trip_ascii(self):
        key = _random_key()
        blob = encrypt("hello world", key)
        self.assertEqual(decrypt(blob, key), "hello world")

    def test_round_trip_empty(self):
        key = _random_key()
        blob = encrypt("", key)
        self.assertEqual(decrypt(blob, key), "")

    def test_round_trip_unicode(self):
        key = _random_key()
        plaintext = "hélïpö — 日本語 — 🎉"
        blob = encrypt(plaintext, key)
        self.assertEqual(decrypt(blob, key), plaintext)

    def test_round_trip_large(self):
        key = _random_key()
        plaintext = "x" * 100_000
        blob = encrypt(plaintext, key)
        self.assertEqual(decrypt(blob, key), plaintext)

    def test_blob_is_base64(self):
        """The encrypted blob must be valid base64."""
        key = _random_key()
        blob = encrypt("test", key)
        # Must not raise.
        base64.b64decode(blob)

    def test_nonce_is_random(self):
        """Two encryptions of the same plaintext must produce different blobs."""
        key = _random_key()
        blob1 = encrypt("same", key)
        blob2 = encrypt("same", key)
        self.assertNotEqual(blob1, blob2)


class TestDecryptErrors(unittest.TestCase):
    """TRIANGULATE: decrypt rejects tampered or malformed blobs."""

    def test_tampered_tag_raises(self):
        key = _random_key()
        blob = encrypt("secret", key)
        # Flip the last byte of the base64-decoded blob.
        raw = bytearray(base64.b64decode(blob))
        raw[-1] ^= 0xFF
        tampered = base64.b64encode(raw).decode()
        with self.assertRaises(Exception):
            decrypt(tampered, key)

    def test_wrong_key_raises(self):
        key1 = _random_key()
        key2 = _random_key()
        blob = encrypt("secret", key1)
        with self.assertRaises(Exception):
            decrypt(blob, key2)

    def test_truncated_blob_raises(self):
        key = _random_key()
        with self.assertRaises(Exception):
            decrypt("dG9v", key)  # too short to contain nonce

    def test_invalid_base64_raises(self):
        key = _random_key()
        with self.assertRaises(Exception):
            decrypt("not!valid!base64!!", key)


class TestParseKey(unittest.TestCase):
    """parse_key validates and converts the CLIPTUNNEL_AES_KEY env value."""

    def test_valid_key(self):
        key_bytes = _random_key()
        encoded = base64.b64encode(key_bytes).decode()
        parsed = parse_key(encoded)
        self.assertEqual(parsed, key_bytes)

    def test_wrong_length_raises(self):
        short_key = base64.b64encode(os.urandom(16)).decode()
        with self.assertRaises(ValueError):
            parse_key(short_key)

    def test_invalid_base64_raises(self):
        with self.assertRaises(ValueError):
            parse_key("not!base64!!")

    def test_empty_string_raises(self):
        with self.assertRaises(ValueError):
            parse_key("")


class TestKeyCompatibility(unittest.TestCase):
    """TRIANGULATE: keys from parse_key work with encrypt/decrypt."""

    def test_parsed_key_round_trips(self):
        key_bytes = _random_key()
        encoded = base64.b64encode(key_bytes).decode()
        key = parse_key(encoded)
        blob = encrypt("data", key)
        self.assertEqual(decrypt(blob, key), "data")


if __name__ == "__main__":
    unittest.main()