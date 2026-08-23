"""Tests for build_transport() factory — T4.

Converted to unittest.TestCase so the CI runner (unittest discover) can
discover and run these tests without pytest installed.
"""
from __future__ import annotations

import base64
import os
import unittest

from cliptunnel_mcp.clipboard_transport import ClipboardTransport
from cliptunnel_mcp.encrypted_transport import EncryptedTransport
from cliptunnel_mcp.https_transport import HttpsTransport
from cliptunnel_mcp.transport_factory import build_transport


def _clipboard_available() -> bool:
    """True if a real ClipboardTransport can be constructed on this host.

    Headless Linux CI runners have no clipboard backend, so clipboard
    transport tests must be skipped there."""
    try:
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport

        t = ClipboardTransport()
        t.close()
        return True
    except Exception:
        return False


_CLIPBOARD_OK = _clipboard_available()


class _EnvGuard:

    def __init__(self) -> None:
        self._saved: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key, "")
        os.environ[key] = value

    def delete(self, key: str) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key, "")
        os.environ.pop(key, None)

    def restore(self) -> None:
        for key, value in self._saved.items():
            if value == "" and key not in os.environ:
                continue
            os.environ[key] = value


class TestDefaults(unittest.TestCase):
    @unittest.skipUnless(_CLIPBOARD_OK, "clipboard backend not available on this host")
    def test_no_env_returns_clipboard(self) -> None:
        env = _EnvGuard()
        try:
            env.delete("CLIPTUNNEL_TRANSPORT")
            t = build_transport()
            self.assertIsInstance(t, ClipboardTransport)
            t.close()
        finally:
            env.restore()

    @unittest.skipUnless(_CLIPBOARD_OK, "clipboard backend not available on this host")
    def test_explicit_clipboard(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "clipboard")
            t = build_transport()
            self.assertIsInstance(t, ClipboardTransport)
            t.close()
        finally:
            env.restore()


class TestHttps(unittest.TestCase):
    def test_https_returns_encrypted_or_plain(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, HttpsTransport)
            t.close()
        finally:
            env.restore()

    def test_https_rejects_http_url(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "http://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("https", str(ctx.exception))
        finally:
            env.restore()

    def test_https_missing_url_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.delete("CLIPTUNNEL_REPEATER_URL")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_REPEATER_URL", str(ctx.exception))
        finally:
            env.restore()

    def test_https_missing_token_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.delete("CLIPTUNNEL_REPEATER_TOKEN")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_REPEATER_TOKEN", str(ctx.exception))
        finally:
            env.restore()

    def test_https_missing_both_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.delete("CLIPTUNNEL_REPEATER_URL")
            env.delete("CLIPTUNNEL_REPEATER_TOKEN")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_REPEATER_URL", str(ctx.exception))
        finally:
            env.restore()

    def test_https_case_insensitive(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "HTTPS")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, HttpsTransport)
            t.close()
        finally:
            env.restore()


class TestAESKey(unittest.TestCase):
    def test_valid_aes_key_wraps_in_encrypted(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 32).decode())
            t = build_transport()
            self.assertIsInstance(t, EncryptedTransport)
            t.close()
        finally:
            env.restore()

    def test_wrong_length_aes_key_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 16).decode())
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("32 bytes", str(ctx.exception))
        finally:
            env.restore()

    def test_bad_base64_aes_key_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", "!!!not-base64!!!")
            with self.assertRaises(ValueError):
                build_transport()
        finally:
            env.restore()


class TestUnknown(unittest.TestCase):
    def test_unknown_transport_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("not supported", str(ctx.exception))
        finally:
            env.restore()


if __name__ == "__main__":
    unittest.main()