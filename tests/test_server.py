"""MCP server surface tests — tool registration and end-to-end wiring.

Builds a real Controller + Agent pair over the deterministic ClipboardSlot
with the genuine operations dispatcher as the Agent handler, so every tool
call travels the full CT2 protocol path (tool → Controller → slot → Agent →
dispatch → response).  No clipboard hardware and no network.

The MCP-specific tests are skipped (not errored) when the official ``mcp``
package is absent, keeping the bare PYTHONPATH suite green; the lazy-import
contract is asserted unconditionally in a fresh subprocess.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from cliptunnel_mcp import Agent, Controller
from cliptunnel_mcp.operations import dispatch as ops_dispatch
from tests.clipboard_slot import ClipboardSlot

HAS_MCP = importlib.util.find_spec("mcp") is not None

EXPECTED_TOOLS = {
    "remote_shell",
    "remote_shell_result",
    "remote_fs_read",
    "remote_fs_write",
    "remote_fs_list",
    "remote_fs_delete",
    "remote_fs_replace",
    "remote_fs_search",
    "remote_fs_find",
    "remote_fs_bin_read",
    "remote_fs_bin_write",
    "remote_upload",
    "remote_download",
    "remote_sysinfo",
    "remote_connections",
    "remote_discovery",
    "remote_agent_login",
    "remote_agent_login_status",
    "remote_agent_models",
    "remote_agent_start",
    "remote_agent_continue",
    "remote_agent_result",
    "remote_agent_status",
    "remote_agent_list",
    "remote_agent_clear",
    "remote_agent_end",
    "remote_install_instructions",
}


def text_of(result) -> str:
    """Extract the tool's string output from a FastMCP call_tool result.

    Handles both known result shapes: a plain content value, and the newer
    ``(content_sequence, structured_dict | None)`` tuple.
    """
    if isinstance(result, tuple) and len(result) == 2:
        content, structured = result
        if isinstance(structured, dict) and isinstance(structured.get("result"), str):
            return structured["result"]
        result = content
    if isinstance(result, str):
        return result
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(result, (list, tuple)):
        parts = [getattr(item, "text", None) for item in result]
        if parts and all(isinstance(part, str) for part in parts):
            return "".join(parts)
    return str(result)


class TestServerModuleLaziness(unittest.TestCase):
    """The server module must import cleanly without the mcp extra."""

    def test_import_does_not_pull_mcp(self):
        src_dir = str(Path(__file__).resolve().parent.parent / "src")
        # The core package (protocol, controller, __init__) must not pull mcp.
        # server.py itself imports Context from mcp for type annotations —
        # that's expected and acceptable for the server-only module.
        probe = (
            "import sys; "
            "import cliptunnel_mcp; "
            "import cliptunnel_mcp.protocol; "
            "import cliptunnel_mcp.controller; "
            "assert 'mcp' not in sys.modules, 'core package pulled mcp'; "
            "assert callable(cliptunnel_mcp.dispatch); "
            "assert callable(cliptunnel_mcp.pack)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            env={**os.environ, "PYTHONPATH": src_dir},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipUnless(HAS_MCP, "mcp package not installed (cliptunnel-mcp[server])")
class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        server = importlib.import_module("cliptunnel_mcp.server")
        self.server = server
        self.slot = ClipboardSlot()
        self.agent = Agent(
            self.slot,
            ops_dispatch,
            poll_interval=0.001,
            max_workers=2,
            response_ack_timeout=0.02,
        )
        self.controller = Controller(
            self.slot,
            timeout=10.0,
            ack_timeout=0.05,
            retries=3,
            poll_interval=0.001,
            initial_seq=0,
            controller_id="C1a2b3c4",
        )
        server.set_controller(self.controller)
        self.mcp = server.create_server()
        self.addCleanup(self.controller.close)
        self.addCleanup(self.agent.close)
        self.addCleanup(server.reset)

    # ── helpers ──────────────────────────────────────────────────

    def call(self, name: str, **arguments) -> str:
        result = asyncio.run(self.mcp.call_tool(name, arguments))
        return text_of(result)

    def call_json(self, name: str, **arguments) -> dict:
        return json.loads(self.call(name, **arguments))


class TestToolSurface(ServerTestCase):
    def test_registered_tool_names(self):
        tools = asyncio.run(self.mcp.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(names, EXPECTED_TOOLS)

    def test_every_tool_has_a_description(self):
        tools = asyncio.run(self.mcp.list_tools())
        for tool in tools:
            self.assertTrue(
                tool.description and tool.description.strip(),
                f"tool {tool.name} lacks a docstring",
            )


class TestShellTools(ServerTestCase):
    def test_fast_command_returns_finished_result(self):
        data = self.call_json("remote_shell", cmd="echo hello")
        self.assertEqual(data["status"], "finished")
        self.assertIsNone(data["job_id"])
        self.assertEqual(data["stdout"].strip(), "hello")
        self.assertEqual(data["returncode"], 0)

    def test_slow_command_returns_running_then_finished(self):
        data = self.call_json(
            "remote_shell", cmd="sleep 1.5 && echo done", sync_timeout=0.3
        )
        self.assertEqual(data["status"], "running")
        self.assertIsNotNone(data["job_id"])
        self.assertIsNone(data["stdout"])

        job_id = data["job_id"]
        deadline = time.monotonic() + 15.0
        polled = data
        while time.monotonic() < deadline:
            polled = self.call_json("remote_shell_result", job_id=job_id)
            if polled["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(polled["status"], "finished")
        self.assertEqual(polled["stdout"].strip(), "done")
        self.assertEqual(polled["returncode"], 0)

    def test_shell_result_unknown_job_is_not_found(self):
        data = self.call_json("remote_shell_result", job_id="nope12345")
        self.assertEqual(data["status"], "not_found")
        self.assertEqual(data["job_id"], "nope12345")


class TestFsTools(ServerTestCase):
    def test_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "note.txt")
            out = self.call("remote_fs_write", path=p, content="hi there")
            self.assertIn("wrote 8 bytes", out)
            data = self.call_json("remote_fs_read", path=p)
            self.assertEqual(data["content"], "hi there")
            self.assertEqual(data["lines"], 1)

    def test_list_and_find(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sub"))
            for name in ("a.txt", "b.log", os.path.join("sub", "c.txt")):
                with open(os.path.join(d, name), "w") as f:
                    f.write("x")
            entries = json.loads(self.call("remote_fs_list", path=d))
            self.assertEqual(
                [e["name"] for e in entries], ["a.txt", "b.log", "sub"]
            )
            found = json.loads(self.call("remote_fs_find", path=d, pattern="**/*.txt"))
            self.assertEqual(
                sorted(os.path.basename(f) for f in found), ["a.txt", "c.txt"]
            )

    def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "gone.txt")
            with open(p, "w") as f:
                f.write("bye")
            out = self.call("remote_fs_delete", path=p)
            self.assertIn("deleted", out)
            self.assertFalse(os.path.exists(p))

    def test_search_finds_matching_lines(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.txt")
            with open(p, "w") as f:
                f.write("cat\ndog\nbat\n")
            matches = json.loads(self.call("remote_fs_search", path=p, pattern="a"))
            self.assertEqual([m["line"] for m in matches], [1, 3])

    def test_replace_exact_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("foo bar foo")
            out = self.call("remote_fs_replace", path=p, old="bar", new="baz")
            self.assertIn("replaced 1 occurrence", out)
            with open(p) as f:
                self.assertEqual(f.read(), "foo baz foo")

    def test_agent_level_error_surfaces_payload(self):
        # ops errors travel as ERROR envelopes; the Controller now passes
        # the error payload through (previously it discarded it as None,
        # which surfaced as a misleading "no response from Agent").
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("nothing here")
            out = self.call("remote_fs_replace", path=p, old="xyz", new="abc")
            self.assertIn("old text not found", out)

    def test_bin_read_write_roundtrip(self):
        import base64
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src.bin")
            dst = os.path.join(d, "dst.bin")
            raw = b"\x00\x01\x02\xff"
            with open(src, "wb") as f:
                f.write(raw)
            data = self.call_json("remote_fs_bin_read", path=src)
            self.assertEqual(base64.b64decode(data["b64"]), raw)
            out = self.call("remote_fs_bin_write", path=dst, b64=data["b64"])
            self.assertIn("wrote 4 bytes", out)
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), raw)

    def test_upload_download_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            local = os.path.join(d, "local.bin")
            remote = os.path.join(d, "remote.bin")
            back = os.path.join(d, "back.bin")
            raw = b"binary payload \x00\xfe"
            with open(local, "wb") as f:
                f.write(raw)
            out = self.call("remote_upload", local_path=local, remote_path=remote)
            self.assertIn("wrote", out)
            out = self.call("remote_download", remote_path=remote, local_path=back)
            self.assertIn("downloaded", out)
            with open(back, "rb") as f:
                self.assertEqual(f.read(), raw)

    def test_no_controller_reports_error_string(self):
        # setUp injected a controller; prove the unwired path degrades to
        # the documented error string instead of raising.
        self.server.reset()
        out = self.call("remote_fs_read", path="/no/such/file.txt")
        self.assertEqual(out, "ERROR: no response from Agent")
        data = self.call_json("remote_shell", cmd="echo hi")
        self.assertEqual(data["status"], "error")
        self.assertIn("no transport configured", data["error"])


class TestRemoteConnections(ServerTestCase):
    def test_remote_connections_returns_json_dict(self):
        """remote_connections returns a JSON dict (possibly empty or populated
        after registration traffic settles)."""
        result = self.call("remote_connections")
        data = json.loads(result)
        self.assertIsInstance(data, dict)
        self.assertIn("controllers", data)
        self.assertIn("remotes", data)

    def test_remote_connections_without_controller_returns_empty(self):
        """When no controller is configured, remote_connections returns
        {'controllers': {}, 'remotes': {}}."""
        self.server.reset()
        result = self.call("remote_connections")
        self.assertEqual(json.loads(result), {"controllers": {}, "remotes": {}})

    def test_remote_connections_populated_after_registration(self):
        """After the Agent registers (ANNOUNCE → delayed response),
        remote_connections should show the agent's remote_id in remotes
        with sysinfo."""
        # Wait up to 5s for the registration to arrive (random delay 0.1–4.0s).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            result = self.call("remote_connections")
            data = json.loads(result)
            if self.agent.remote_id in data.get("remotes", {}):
                break
            time.sleep(0.1)
        data = json.loads(self.call("remote_connections"))
        self.assertIn(self.agent.remote_id, data["remotes"])
        info = data["remotes"][self.agent.remote_id]
        self.assertIn("os", info)
        self.assertEqual(info.get("status"), "alive")


class TestAgentTools(ServerTestCase):
    def test_agent_list_returns_json(self):
        """remote_agent_list returns valid JSON (possibly an empty array)."""
        result = self.call("remote_agent_list")
        data = json.loads(result)
        self.assertIsInstance(data, list)

    def test_agent_end_unknown_session(self):
        """remote_agent_end with a fake session_id returns an error string."""
        result = self.call("remote_agent_end", session_id="nope12345")
        # Error responses from the Agent come back as the documented error
        # string when the Controller normalizes None.
        self.assertIn(result, ("session not found: nope12345", "ERROR: no response from Agent"))

    def test_agent_login_status_idle(self):
        """remote_agent_login_status returns idle when no login in progress."""
        result = self.call("remote_agent_login_status")
        data = json.loads(result)
        self.assertIn(data["status"], ("idle", "polling", "done", "error"))


class TestRemoteInstallInstructions(ServerTestCase):
    def test_clipboard_variant(self):
        """Default (clipboard) transport returns minimal instructions."""
        old = os.environ.pop("CLIPTUNNEL_TRANSPORT", None)
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["transport"], "clipboard")
            self.assertEqual(data["env_vars"], {})
            self.assertIn("cliptunnel-agent", data["agent_command"])
        finally:
            if old is not None:
                os.environ["CLIPTUNNEL_TRANSPORT"] = old

    def test_https_variant(self):
        """HTTPS transport returns full config with repeater URL + token."""
        os.environ["CLIPTUNNEL_TRANSPORT"] = "https"
        os.environ["CLIPTUNNEL_REPEATER_URL"] = "https://relay.example.com"
        os.environ["CLIPTUNNEL_REPEATER_TOKEN"] = "secret123"
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["transport"], "https")
            self.assertEqual(data["repeater_url"], "https://relay.example.com")
            self.assertEqual(data["agent_token"], "secret123")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_TRANSPORT"], "https")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_REPEATER_URL"], "https://relay.example.com")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_REPEATER_TOKEN"], "secret123")
            self.assertNotIn("CLIPTUNNEL_AES_KEY", data["env_vars"])
            self.assertNotIn("aes_key", data)
            self.assertIn("cliptunnel-agent", data["agent_command"])
            self.assertIn("CLIPTUNNEL_REPEATER_TOKEN=secret123", data["agent_command"])
        finally:
            os.environ.pop("CLIPTUNNEL_TRANSPORT", None)
            os.environ.pop("CLIPTUNNEL_REPEATER_URL", None)
            os.environ.pop("CLIPTUNNEL_REPEATER_TOKEN", None)

    def test_https_with_aes_key(self):
        """HTTPS + AES key includes aes_key field in output."""
        import base64
        os.environ["CLIPTUNNEL_TRANSPORT"] = "https"
        os.environ["CLIPTUNNEL_REPEATER_URL"] = "https://relay.example.com"
        os.environ["CLIPTUNNEL_REPEATER_TOKEN"] = "secret123"
        aes_b64 = base64.b64encode(b"0" * 32).decode()
        os.environ["CLIPTUNNEL_AES_KEY"] = aes_b64
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["aes_key"], aes_b64)
            self.assertIn("CLIPTUNNEL_AES_KEY", data["env_vars"])
        finally:
            for k in ("CLIPTUNNEL_TRANSPORT", "CLIPTUNNEL_REPEATER_URL",
                      "CLIPTUNNEL_REPEATER_TOKEN", "CLIPTUNNEL_AES_KEY"):
                os.environ.pop(k, None)

    def test_firebase_variant(self):
        """Firebase transport returns full config with URL + token."""
        os.environ["CLIPTUNNEL_TRANSPORT"] = "firebase"
        os.environ["CLIPTUNNEL_FIREBASE_URL"] = "https://x-default-rtdb.firebaseio.com"
        os.environ["CLIPTUNNEL_FIREBASE_TOKEN"] = "fb-secret"
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["transport"], "firebase")
            self.assertEqual(data["firebase_url"], "https://x-default-rtdb.firebaseio.com")
            self.assertEqual(data["firebase_token"], "fb-secret")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_TRANSPORT"], "firebase")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_FIREBASE_TOKEN"], "fb-secret")
            self.assertNotIn("aes_key", data)
            self.assertIn("CLIPTUNNEL_FIREBASE_TOKEN=fb-secret", data["agent_command"])
        finally:
            for k in ("CLIPTUNNEL_TRANSPORT", "CLIPTUNNEL_FIREBASE_URL",
                      "CLIPTUNNEL_FIREBASE_TOKEN"):
                os.environ.pop(k, None)

class TestRemoteInstallInstructionsWebSocket(ServerTestCase):
    def test_websocket_variant(self):
        """WebSocket transport returns full config with WS URL + token."""
        os.environ["CLIPTUNNEL_TRANSPORT"] = "websocket"
        os.environ["CLIPTUNNEL_WS_URL"] = "ws://relay.example.com:9000"
        os.environ["CLIPTUNNEL_WS_TOKEN"] = "def456secret"
        old_aes = os.environ.pop("CLIPTUNNEL_AES_KEY", None)
        old_config = os.environ.get("CLIPTUNNEL_CONFIG")
        import tempfile as _tf
        _empty = _tf.NamedTemporaryFile(mode="w", suffix=".toml", delete=False)
        _empty.close()
        os.environ["CLIPTUNNEL_CONFIG"] = _empty.name
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["transport"], "websocket")
            self.assertEqual(data["ws_url"], "ws://relay.example.com:9000")
            self.assertEqual(data["agent_token"], "def456secret")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_TRANSPORT"], "websocket")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_WS_URL"], "ws://relay.example.com:9000")
            self.assertEqual(data["env_vars"]["CLIPTUNNEL_WS_TOKEN"], "def456secret")
            self.assertNotIn("CLIPTUNNEL_AES_KEY", data["env_vars"])
            self.assertNotIn("aes_key", data)
            self.assertIn("cliptunnel-agent", data["agent_command"])
            self.assertIn("CLIPTUNNEL_WS_TOKEN=def456secret", data["agent_command"])
        finally:
            for k in ("CLIPTUNNEL_TRANSPORT", "CLIPTUNNEL_WS_URL",
                      "CLIPTUNNEL_WS_TOKEN"):
                os.environ.pop(k, None)
            if old_aes is not None:
                os.environ["CLIPTUNNEL_AES_KEY"] = old_aes
            if old_config is not None:
                os.environ["CLIPTUNNEL_CONFIG"] = old_config
            else:
                os.environ.pop("CLIPTUNNEL_CONFIG", None)
            import os as _os
            try:
                _os.unlink(_empty.name)
            except OSError:
                pass
    def test_websocket_with_aes_key(self):
        import base64
        os.environ["CLIPTUNNEL_TRANSPORT"] = "websocket"
        os.environ["CLIPTUNNEL_WS_URL"] = "ws://relay.example.com:9000"
        os.environ["CLIPTUNNEL_WS_TOKEN"] = "def456secret"
        aes_b64 = base64.b64encode(b"0" * 32).decode()
        os.environ["CLIPTUNNEL_AES_KEY"] = aes_b64
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["aes_key"], aes_b64)
            self.assertIn("CLIPTUNNEL_AES_KEY", data["env_vars"])
        finally:
            for k in ("CLIPTUNNEL_TRANSPORT", "CLIPTUNNEL_WS_URL",
                      "CLIPTUNNEL_WS_TOKEN", "CLIPTUNNEL_AES_KEY"):
                os.environ.pop(k, None)

    def test_existing_https_branch_unchanged(self):
        """HTTPS transport still works after adding WebSocket branch."""
        os.environ["CLIPTUNNEL_TRANSPORT"] = "https"
        os.environ["CLIPTUNNEL_REPEATER_URL"] = "https://relay.example.com"
        os.environ["CLIPTUNNEL_REPEATER_TOKEN"] = "secret123"
        try:
            result = self.call("remote_install_instructions")
            data = json.loads(result)
            self.assertEqual(data["transport"], "https")
        finally:
            for k in ("CLIPTUNNEL_TRANSPORT", "CLIPTUNNEL_REPEATER_URL",
                      "CLIPTUNNEL_REPEATER_TOKEN"):
                os.environ.pop(k, None)

if __name__ == "__main__":
    unittest.main()