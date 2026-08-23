"""Tests for build_transport() factory — T4.

Converted to unittest.TestCase so the CI runner (unittest discover) can
discover and run these tests without pytest installed.
"""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

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


class TestConfigFile(unittest.TestCase):
    """build_transport() must work purely from a config.toml file."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Clear every CLIPTUNNEL_* env var so only the file can supply settings.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (
            "CLIPTUNNEL_TRANSPORT",
            "CLIPTUNNEL_REPEATER_URL",
            "CLIPTUNNEL_REPEATER_TOKEN",
            "CLIPTUNNEL_AES_KEY",
        ):
            os.environ.pop(name, None)
        # Point the config layer at a temp file via CLIPTUNNEL_CONFIG.
        self.config_path = os.path.join(self._tmp.name, "config.toml")
        os.environ["CLIPTUNNEL_CONFIG"] = self.config_path

    def _write(self, content: str) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_https_built_from_config_file_only(self) -> None:
        """A non-https URL in the file proves the file was consumed."""
        self._write(
            '[transport]\n'
            'type = "https"\n'
            'repeater_url = "http://repeater.example.com"\n'
            'repeater_token = "tok"\n'
        )
        with self.assertRaises(ValueError) as ctx:
            build_transport()
        self.assertIn("https scheme", str(ctx.exception))

    def test_https_constructed_from_config_file_and_closed(self) -> None:
        """Valid https URL from the file builds HttpsTransport; close immediately."""
        self._write(
            '[transport]\n'
            'type = "https"\n'
            'repeater_url = "https://127.0.0.1:1"\n'
            'repeater_token = "tok"\n'
        )
        # mock.patch.dict (setUp) restores env on teardown.
        transport = build_transport()
        self.assertIsInstance(transport, HttpsTransport)
        transport.close()


if __name__ == "__main__":
    unittest.main()