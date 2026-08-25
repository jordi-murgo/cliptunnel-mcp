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
from cliptunnel_mcp.firebase_transport import FirebaseTransport
from cliptunnel_mcp.https_transport import HttpsTransport
from cliptunnel_mcp.ws_transport import WebSocketTransport
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
        # Point CLIPTUNNEL_CONFIG to an empty file so the default
        # ~/.cliptunnel/config.toml doesn't interfere with factory tests.
        import tempfile
        self._saved.setdefault("CLIPTUNNEL_CONFIG", os.environ.get("CLIPTUNNEL_CONFIG", ""))
        self._empty_config = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", delete=False)
        self._empty_config.close()
        os.environ["CLIPTUNNEL_CONFIG"] = self._empty_config.name

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
        try:
            os.unlink(self._empty_config.name)
        except OSError:
            pass

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
    def test_valid_aes_key_returns_raw_transport(self) -> None:
        """AES key is handled at protocol level now; factory returns raw transport."""
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 32).decode())
            t = build_transport()
            self.assertIsInstance(t, HttpsTransport)
            t.close()
        finally:
            env.restore()

    def test_aes_key_does_not_affect_factory(self) -> None:
        """Even a bad AES key does not raise in the factory (handled later)."""
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 16).decode())
            t = build_transport()
            self.assertIsInstance(t, HttpsTransport)
            t.close()
        finally:
            env.restore()

class TestUnknown(unittest.TestCase):
    def test_unknown_transport_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            msg = str(ctx.exception)
            self.assertIn("not supported", msg)
            self.assertIn("Available transports:", msg)
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
            "CLIPTUNNEL_FIREBASE_URL",
            "CLIPTUNNEL_FIREBASE_TOKEN",
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


class TestFirebaseConfigFile(unittest.TestCase):
    """build_transport() must support 'firebase' purely from a config file."""

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
            "CLIPTUNNEL_FIREBASE_URL",
            "CLIPTUNNEL_FIREBASE_TOKEN",
            "CLIPTUNNEL_AES_KEY",
        ):
            os.environ.pop(name, None)
        self.config_path = os.path.join(self._tmp.name, "config.toml")
        os.environ["CLIPTUNNEL_CONFIG"] = self.config_path

    def _write(self, content: str) -> None:
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(content)

    def test_firebase_constructed_from_config_file_and_closed(self) -> None:
        """Valid firebase settings from the file build FirebaseTransport."""
        self._write(
            '[transport]\n'
            'type = "firebase"\n'
            'firebase_url = "https://127.0.0.1:1"\n'
            'firebase_token = "tok"\n'
        )
        transport = build_transport()
        self.assertIsInstance(transport, FirebaseTransport)
        transport.close()

    def test_firebase_missing_url_raises(self) -> None:
        self._write(
            '[transport]\n'
            'type = "firebase"\n'
            'firebase_token = "tok"\n'
        )
        with self.assertRaises(ValueError) as ctx:
            build_transport()
        self.assertIn("CLIPTUNNEL_FIREBASE_URL", str(ctx.exception))

    def test_firebase_missing_token_raises(self) -> None:
        self._write(
            '[transport]\n'
            'type = "firebase"\n'
            'firebase_url = "https://127.0.0.1:1"\n'
        )
        with self.assertRaises(ValueError) as ctx:
            build_transport()
        self.assertIn("CLIPTUNNEL_FIREBASE_TOKEN", str(ctx.exception))

    def test_firebase_missing_both_names_both(self) -> None:
        self._write('[transport]\ntype = "firebase"\n')
        with self.assertRaises(ValueError) as ctx:
            build_transport()
        message = str(ctx.exception)
        self.assertIn("CLIPTUNNEL_FIREBASE_URL", message)
        self.assertIn("CLIPTUNNEL_FIREBASE_TOKEN", message)

    def test_firebase_rejects_http_url(self) -> None:
        self._write(
            '[transport]\n'
            'type = "firebase"\n'
            'firebase_url = "http://127.0.0.1:1"\n'
            'firebase_token = "tok"\n'
        )
        with self.assertRaises(ValueError) as ctx:
            build_transport()
        self.assertIn("https scheme", str(ctx.exception))


class TestFirebaseEnv(unittest.TestCase):
    def test_firebase_case_insensitive(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "FIREBASE")
            env.set("CLIPTUNNEL_FIREBASE_URL", "https://127.0.0.1:1")
            env.set("CLIPTUNNEL_FIREBASE_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, FirebaseTransport)
            t.close()
        finally:
            env.restore()

    def test_firebase_missing_url_from_env_names_var(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "firebase")
            env.delete("CLIPTUNNEL_FIREBASE_URL")
            env.set("CLIPTUNNEL_FIREBASE_TOKEN", "secret")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_FIREBASE_URL", str(ctx.exception))
        finally:
            env.restore()



class TestWebSocketValidation(unittest.TestCase):
    """Tests that validate env vars without creating a transport (no websockets needed)."""

    def test_websocket_missing_url_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.delete("CLIPTUNNEL_WS_URL")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_WS_URL", str(ctx.exception))
        finally:
            env.restore()

    def test_websocket_missing_token_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.set("CLIPTUNNEL_WS_URL", "ws://relay.example.com:9000")
            env.delete("CLIPTUNNEL_WS_TOKEN")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_WS_TOKEN", str(ctx.exception))
        finally:
            env.restore()

    def test_websocket_missing_both_raises(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.delete("CLIPTUNNEL_WS_URL")
            env.delete("CLIPTUNNEL_WS_TOKEN")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("CLIPTUNNEL_WS_URL", str(ctx.exception))
        finally:
            env.restore()

    def test_websocket_rejects_http_scheme(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.set("CLIPTUNNEL_WS_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("ws://", str(ctx.exception))
        finally:
            env.restore()

    def test_unknown_transport_includes_websocket_in_message(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            self.assertIn("websocket", str(ctx.exception))
        finally:
            env.restore()


class TestWebSocket(unittest.TestCase):
    def test_websocket_returns_ws_transport(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.set("CLIPTUNNEL_WS_URL", "ws://relay.example.com:9000")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, WebSocketTransport)
            t.close()
        finally:
            env.restore()
    def test_websocket_case_insensitive(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "WEBSOCKET")
            env.set("CLIPTUNNEL_WS_URL", "ws://relay.example.com:9000")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, WebSocketTransport)
            t.close()
        finally:
            env.restore()

    def test_websocket_wss_scheme_accepted(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.set("CLIPTUNNEL_WS_URL", "wss://relay.example.com")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            t = build_transport()
            self.assertIsInstance(t, WebSocketTransport)
            t.close()
        finally:
            env.restore()

    def test_websocket_with_aes_key_returns_raw_transport(self) -> None:
        import base64
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "websocket")
            env.set("CLIPTUNNEL_WS_URL", "ws://relay.example.com:9000")
            env.set("CLIPTUNNEL_WS_TOKEN", "secret")
            env.set("CLIPTUNNEL_AES_KEY", base64.b64encode(b"0" * 32).decode())
            t = build_transport()
            self.assertIsInstance(t, WebSocketTransport)
            t.close()
        finally:
            env.restore()
    def test_existing_https_branch_unchanged(self) -> None:
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "https")
            env.set("CLIPTUNNEL_REPEATER_URL", "https://relay.example.com")
            env.set("CLIPTUNNEL_REPEATER_TOKEN", "secret")
            from cliptunnel_mcp.https_transport import HttpsTransport
            t = build_transport()
            self.assertIsInstance(t, HttpsTransport)
            t.close()
        finally:
            env.restore()

class TestRegistryLookup(unittest.TestCase):
    """T3: build_transport() uses registry for transport lookup."""

    def test_unknown_transport_lists_registry_names(self) -> None:
        """Unknown transport error must list registry.transport_names()."""
        env = _EnvGuard()
        try:
            env.set("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
            with self.assertRaises(ValueError) as ctx:
                build_transport()
            msg = str(ctx.exception)
            self.assertIn("not supported", msg)
            self.assertIn("Available transports:", msg)
            # Registry has clipboard, https, firebase, websocket
            for name in ("clipboard", "https", "firebase", "websocket"):
                self.assertIn(name, msg)
        finally:
            env.restore()

    def test_registry_loaded_on_build(self) -> None:
        """build_transport() ensures register_builtins has run."""
        from cliptunnel_mcp import plugins
        # Reset _loaded flag to verify build_transport triggers load
        old_loaded = plugins._loaded
        plugins._loaded = False
        try:
            env = _EnvGuard()
            try:
                env.set("CLIPTUNNEL_TRANSPORT", "carrier-pigeon")
                with self.assertRaises(ValueError):
                    build_transport()
                # After calling build_transport, _loaded should be True
                self.assertTrue(plugins._loaded)
            finally:
                env.restore()
        finally:
            plugins._loaded = old_loaded

if __name__ == "__main__":
    unittest.main()