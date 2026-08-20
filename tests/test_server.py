"""MCP server surface tests — tool registration and end-to-end wiring.

Builds a real Controller + Agent pair over the deterministic ClipboardSlot
with the genuine operations dispatcher as the Agent handler, so every tool
call travels the full CT1 protocol path (tool → Controller → slot → Agent →
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
        probe = (
            "import sys; import cliptunnel_mcp.server as s; "
            "assert 'mcp' not in sys.modules, 'server import pulled mcp'; "
            "assert callable(s.create_server) and callable(s.main); "
            "import cliptunnel_mcp; assert callable(cliptunnel_mcp.dispatch)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            env={**os.environ, "PYTHONPATH": src_dir},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode, 0, f"lazy import probe failed:\n{proc.stderr}"
        )


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

    def test_agent_level_error_surfaces_as_no_response(self):
        # ops errors travel as ERROR envelopes; the Controller resolves them
        # to None and _send normalizes — the faithful vulcano behavior.
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("nothing here")
            out = self.call("remote_fs_replace", path=p, old="xyz", new="abc")
            self.assertEqual(out, "ERROR: no response from Agent")

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


if __name__ == "__main__":
    unittest.main()
