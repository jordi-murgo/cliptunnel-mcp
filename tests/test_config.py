"""Tests for the TOML config-file layer (src/cliptunnel_mcp/config.py).

Precedence everywhere: environment variable > config file > built-in default.
All tests use tempfile.TemporaryDirectory — the real ``~/.cliptunnel`` is
never touched.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from cliptunnel_mcp import config


_FULL_TOML = """\
[transport]
type = "https"
repeater_url = "https://repeater.example.com"
repeater_token = "tok-123"

[encryption]
aes_key = "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY="

[heartbeat]
interval_secs = 30

[copilot]
oauth_token = "gho_testtoken"
"""

_ALL_ENV_VARS = [
    "CLIPTUNNEL_TRANSPORT",
    "CLIPTUNNEL_REPEATER_URL",
    "CLIPTUNNEL_REPEATER_TOKEN",
    "CLIPTUNNEL_AES_KEY",
    "CLIPTUNNEL_HEARTBEAT_SECS",
    "CLIPTUNNEL_CONFIG",
]


class ConfigTestCase(unittest.TestCase):
    """Base harness: isolated config paths and a clean module state."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Clear every CLIPTUNNEL_* env var and reset module-level overrides.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in _ALL_ENV_VARS:
            os.environ.pop(name, None)
        config.set_config_path(None)

    def write_config(self, content: str, name: str = "config.toml") -> str:
        path = os.path.join(self._tmp.name, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return path


class TestLoadConfig(ConfigTestCase):
    def test_missing_file_returns_empty_dict(self) -> None:
        missing = os.path.join(self._tmp.name, "nope", "config.toml")
        self.assertEqual(config.load_config(missing), {})

    def test_valid_toml_parses_all_sections(self) -> None:
        path = self.write_config(_FULL_TOML)
        cfg = config.load_config(path)
        self.assertEqual(cfg["transport"]["type"], "https")
        self.assertEqual(cfg["transport"]["repeater_url"], "https://repeater.example.com")
        self.assertEqual(cfg["transport"]["repeater_token"], "tok-123")
        self.assertEqual(
            cfg["encryption"]["aes_key"],
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=",
        )
        self.assertEqual(cfg["heartbeat"]["interval_secs"], 30)
        self.assertEqual(cfg["copilot"]["oauth_token"], "gho_testtoken")

    def test_malformed_toml_raises_valueerror_with_path(self) -> None:
        path = self.write_config("not [ valid toml ] at all }}}")
        with self.assertRaises(ValueError) as ctx:
            config.load_config(path)
        self.assertIn(path, str(ctx.exception))

    def test_custom_path_via_env_var(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = path
        cfg = config.load_config()
        self.assertEqual(cfg["copilot"]["oauth_token"], "gho_testtoken")

    def test_explicit_arg_beats_env_var(self) -> None:
        other = self.write_config('[transport]\ntype = "clipboard"\n', name="other.toml")
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = other
        cfg = config.load_config(path)
        self.assertEqual(cfg["transport"]["type"], "https")


class TestPermissionWarning(ConfigTestCase):
    @unittest.skipUnless(os.name == "posix", "permission bits are POSIX-only; Windows always reports 0o666")
    def test_group_world_readable_warns(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.chmod(path, 0o644)
        with self.assertLogs("cliptunnel_mcp.config", level="WARNING") as captured:
            config.load_config(path, force_reload=True)
        joined = "\n".join(captured.output)
        self.assertIn("chmod 600", joined)
        self.assertIn(path, joined)

    @unittest.skipUnless(os.name == "posix", "permission bits are POSIX-only; Windows always reports 0o666")
    def test_owner_only_does_not_warn(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.chmod(path, 0o600)
        with self.assertNoLogs("cliptunnel_mcp.config", level="WARNING"):
            config.load_config(path, force_reload=True)


class TestGetEnv(ConfigTestCase):
    def test_env_var_wins_over_file(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = path
        os.environ["CLIPTUNNEL_TRANSPORT"] = "clipboard"
        self.assertEqual(config.get_env("CLIPTUNNEL_TRANSPORT"), "clipboard")

    def test_file_used_when_env_unset(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = path
        self.assertEqual(config.get_env("CLIPTUNNEL_TRANSPORT"), "https")
        self.assertEqual(config.get_env("CLIPTUNNEL_REPEATER_URL"), "https://repeater.example.com")
        self.assertEqual(config.get_env("CLIPTUNNEL_REPEATER_TOKEN"), "tok-123")
        self.assertEqual(
            config.get_env("CLIPTUNNEL_AES_KEY"),
            "QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWY=",
        )
        self.assertEqual(config.get_env("CLIPTUNNEL_HEARTBEAT_SECS"), "30")

    def test_empty_env_var_falls_through_to_file(self) -> None:
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = path
        os.environ["CLIPTUNNEL_REPEATER_TOKEN"] = ""
        self.assertEqual(config.get_env("CLIPTUNNEL_REPEATER_TOKEN"), "tok-123")

    def test_default_when_neither(self) -> None:
        self.assertIsNone(config.get_env("CLIPTUNNEL_TRANSPORT"))
        self.assertEqual(config.get_env("CLIPTUNNEL_HEARTBEAT_SECS", "120"), "120")

    def test_unmapped_name_behaves_like_environ_get(self) -> None:
        self.assertIsNone(config.get_env("SOME_UNRELATED_VAR"))
        self.assertEqual(config.get_env("SOME_UNRELATED_VAR", "x"), "x")

    def test_set_config_path_beats_cliptunnel_config_env(self) -> None:
        other = self.write_config('[copilot]\noauth_token = "gho_other"\n', name="other.toml")
        path = self.write_config(_FULL_TOML)
        os.environ["CLIPTUNNEL_CONFIG"] = other
        config.set_config_path(path)
        self.assertEqual(config.get_copilot_token(), "gho_testtoken")


class TestGetCopilotToken(ConfigTestCase):
    def test_token_from_file(self) -> None:
        path = self.write_config(_FULL_TOML)
        self.assertEqual(config.get_copilot_token(config_path=path), "gho_testtoken")

    def test_no_token_when_section_missing(self) -> None:
        path = self.write_config('[transport]\ntype = "clipboard"\n')
        self.assertIsNone(config.get_copilot_token(config_path=path))
        self.assertIsNone(config.get_copilot_token())

    def test_blank_token_is_none(self) -> None:
        path = self.write_config('[copilot]\noauth_token = "   "\n')
        self.assertIsNone(config.get_copilot_token(config_path=path))


class TestCacheIsolation(ConfigTestCase):
    def test_cache_refreshes_on_file_change_with_same_path(self) -> None:
        path = self.write_config('[copilot]\noauth_token = "gho_first"\n')
        self.assertEqual(config.get_copilot_token(config_path=path), "gho_first")
        # Rewrite the file; the mtime-keyed cache must see the new content.
        with open(path, "w", encoding="utf-8") as f:
            f.write('[copilot]\noauth_token = "gho_second"\n')
        self.assertEqual(config.get_copilot_token(config_path=path), "gho_second")


class TestEnvToFileWebSocket(unittest.TestCase):
    def test_ws_url_in_env_to_file(self) -> None:
        self.assertIn("CLIPTUNNEL_WS_URL", config.ENV_TO_FILE)
        self.assertEqual(config.ENV_TO_FILE["CLIPTUNNEL_WS_URL"], (("transport",), "ws_url"))

    def test_ws_token_in_env_to_file(self) -> None:
        self.assertIn("CLIPTUNNEL_WS_TOKEN", config.ENV_TO_FILE)
        self.assertEqual(config.ENV_TO_FILE["CLIPTUNNEL_WS_TOKEN"], (("transport",), "ws_token"))

    def test_get_env_ws_url_from_config_file(self) -> None:
        config_content = '''\
[transport]
type = "websocket"
ws_url = "ws://config-file.example.com:9000"
ws_token = "config-token"
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(config_content)
            f.flush()
            config_path = f.name

        old_env = os.environ.pop("CLIPTUNNEL_WS_URL", None)
        old_config = os.environ.get("CLIPTUNNEL_CONFIG")
        os.environ["CLIPTUNNEL_CONFIG"] = config_path
        try:
            result = config.get_env("CLIPTUNNEL_WS_URL", "")
            self.assertEqual(result, "ws://config-file.example.com:9000")
        finally:
            if old_env is not None:
                os.environ["CLIPTUNNEL_WS_URL"] = old_env
            if old_config is not None:
                os.environ["CLIPTUNNEL_CONFIG"] = old_config
            else:
                os.environ.pop("CLIPTUNNEL_CONFIG", None)
            os.unlink(config_path)

    def test_env_var_overrides_config_file_for_ws_url(self) -> None:
        config_content = '''\
[transport]
ws_url = "ws://config.example.com"
'''
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".toml", delete=False
        ) as f:
            f.write(config_content)
            f.flush()
            config_path = f.name

        old_env = os.environ.get("CLIPTUNNEL_WS_URL")
        old_config = os.environ.get("CLIPTUNNEL_CONFIG")
        os.environ["CLIPTUNNEL_WS_URL"] = "ws://env-wins.example.com"
        os.environ["CLIPTUNNEL_CONFIG"] = config_path
        try:
            result = config.get_env("CLIPTUNNEL_WS_URL", "")
            self.assertEqual(result, "ws://env-wins.example.com")
        finally:
            if old_env is not None:
                os.environ["CLIPTUNNEL_WS_URL"] = old_env
            else:
                os.environ.pop("CLIPTUNNEL_WS_URL", None)
            if old_config is not None:
                os.environ["CLIPTUNNEL_CONFIG"] = old_config
            else:
                os.environ.pop("CLIPTUNNEL_CONFIG", None)
            os.unlink(config_path)

    def test_existing_entries_unchanged(self) -> None:
        self.assertEqual(
            config.ENV_TO_FILE["CLIPTUNNEL_REPEATER_URL"], (("transport",), "repeater_url")
        )
        self.assertEqual(
            config.ENV_TO_FILE["CLIPTUNNEL_AES_KEY"], (("encryption",), "aes_key")
        )

if __name__ == "__main__":
    unittest.main()
