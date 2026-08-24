"""ClipTunnel MCP server — exposes Agent operations as MCP tools.

Runs on the operator's machine next to the Controller. Each tool is a thin
wrapper over the shared Controller's structured helpers (ported from
vulcano-helper ``Host``): ``remote_shell`` uses the auto-sync→async job
pattern — it waits up to ``sync_timeout`` seconds (default 10) for the Agent
to answer; slower commands register a background job and return a ``running``
result whose ``job_id`` is polled via ``remote_shell_result``.

The ``mcp`` dependency is imported lazily inside :func:`create_server` and
:func:`main`, so importing :mod:`cliptunnel_mcp` (or this module) never
fails without the ``cliptunnel-mcp[server]`` extra.

Transport: stdio (Claude Desktop, Pi, Cursor, etc.). Logging: stderr only —
stdout is the JSON-RPC channel.

Tool mapping from vulcano-helper ``vdi_mcp_server.py``: ``vdi_shell`` →
``remote_shell``, ``vdi_shell_result`` → ``remote_shell_result``,
``vdi_fs_*`` → ``remote_fs_*``, ``vdi_upload`` → ``remote_upload``,
``vdi_agent_*`` → ``remote_agent_*`` copilot session tools, with GitHub
OAuth Device Flow login. Because the remote peer is the Agent, error
strings say "Agent" where vulcano said "VDI".

This package's core ships no clipboard transport: production callers inject
a wired Controller via :func:`set_controller` before (or while) the server
runs; until then every tool call reports an error string instead of raising.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import logging
import os
import shlex
import sys
import threading
import time
import uuid
from typing import TYPE_CHECKING

# Context is needed at runtime for FastMCP type-annotation resolution.
# This import pulls the mcp package, but only the server module (not the
# core package) depends on mcp — cliptunnel_mcp.protocol and .controller stay clean.
try:
    from mcp.server.fastmcp.server import Context
except ImportError:
    Context = None  # type: ignore[assignment,misc]

from cliptunnel_mcp import config
from cliptunnel_mcp.controller import Controller

logger = logging.getLogger(__name__)

# ── Shared Controller (injected — no default transport in this package) ──────

_controller: Controller | None = None
_controller_lock = threading.Lock()

# Async shell jobs: job_id -> {"future", "cmd", "started_at"} (vulcano Host)
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def set_controller(controller: Controller | None) -> None:
    """Inject the Controller whose transport carries commands to the Agent."""
    global _controller
    with _controller_lock:
        _controller = controller


def _get_controller() -> Controller | None:
    with _controller_lock:
        return _controller

def _capture_client_info(ctx) -> None:
    """Extract client info from the MCP request context and update the controller.

    Called from the first tool invocation — retrieves the client's name,
    version, and protocol version from the MCP InitializeRequestParams and
    updates the Controller's self-identity so it appears in connections.
    """
    controller = _get_controller()
    if controller is None:
        return
    try:
        session = ctx.session
        client_params = getattr(session, "client_params", None)
        if client_params is None:
            return
        client_info = getattr(client_params, "clientInfo", None)
        if client_info is not None:
            client_name = getattr(client_info, "name", None) or "unknown"
            client_version = getattr(client_info, "version", None) or ""
            protocol_version = getattr(client_params, "protocolVersion", None) or ""
            # Update the controller's display name and metadata.
            display = f"{client_name}/{client_version}" if client_version else client_name
            controller.name = display
            with controller._registry_lock:
                existing = controller._controllers.get(controller.controller_id, {})
                existing.update({
                    "name": display,
                    "mcp_protocol_version": str(protocol_version),
                    "mcp_client_name": client_name,
                    "mcp_client_version": client_version,
                    "last_seen": time.time(),
                    "status": "alive",
                })
                controller._controllers[controller.controller_id] = existing
            # No re-announce — the startup announce already reached everyone.
    except Exception:
        logger.debug("could not extract client info from context", exc_info=True)


_client_info_captured = False


def reset() -> None:
    """Drop the injected Controller and all pending shell jobs."""
    set_controller(None)
    with _jobs_lock:
        _jobs.clear()


def _send(fn, *args, **kwargs) -> str:
    """Call a helper and normalize None to an error string."""
    result = fn(*args, **kwargs)
    return result or "ERROR: no response from Agent"


# ── Controller helpers (ported from vulcano-helper Host) ─────────────────────

def shell_auto(cmd: str, sync_timeout: float = 10.0, timeout: float = 60.0, remote_id: str | None = None) -> dict:
    """Send a shell command with auto-sync-then-async behavior.

    Waits up to *sync_timeout* seconds for the Agent to respond. If the
    command completes within that window, returns a finished result
    immediately. Otherwise, registers a background job and returns a
    running result with a job_id for polling via :func:`shell_result`.

    Returns a unified dict:
    - Fast: ``{"job_id": None, "status": "finished", "elapsed": float, "stdout": str, "stderr": str, "returncode": int}``
    - Slow: ``{"job_id": str, "status": "running", "elapsed": float, "stdout": None, "stderr": None, "returncode": None}``
    """
    started = time.monotonic()
    controller = _get_controller()
    if controller is None:
        return {
            "job_id": None,
            "status": "error",
            "elapsed": 0.0,
            "stdout": None,
            "stderr": None,
            "returncode": None,
            "error": "no transport configured (call set_controller first)",
        }
    future = controller.send_command(
        json.dumps({"op": "shell", "cmd": cmd, "timeout": timeout}), remote_id=remote_id
    )
    try:
        result = future.result(timeout=sync_timeout)
        elapsed = time.monotonic() - started
        if result is None:
            return {
                "job_id": None,
                "status": "error",
                "elapsed": round(elapsed, 1),
                "stdout": None,
                "stderr": None,
                "returncode": None,
                "error": "no response from Agent",
            }
        # op_shell returns JSON with stdout/stderr/returncode
        try:
            parsed = json.loads(result)
            return {
                "job_id": None,
                "status": "finished",
                "elapsed": round(elapsed, 1),
                "stdout": parsed.get("stdout", ""),
                "stderr": parsed.get("stderr", ""),
                "returncode": parsed.get("returncode", -1),
            }
        except (json.JSONDecodeError, TypeError):
            # Fallback: treat raw string as stdout
            return {
                "job_id": None,
                "status": "finished",
                "elapsed": round(elapsed, 1),
                "stdout": result,
                "stderr": "",
                "returncode": 0,
            }
    except concurrent.futures.TimeoutError:
        # Command still running — register as async job
        elapsed = time.monotonic() - started
        job_id = uuid.uuid4().hex[:8]
        with _jobs_lock:
            _jobs[job_id] = {
                "future": future,
                "cmd": cmd,
                "started_at": started,
                "timeout": timeout,
            }
        return {
            "job_id": job_id,
            "status": "running",
            "elapsed": round(elapsed, 1),
            "stdout": None,
            "stderr": None,
            "returncode": None,
        }


def shell_result(job_id: str) -> dict:
    """Poll for the result of an async shell command.

    Returns a unified dict:
    - ``{"status": "running", "job_id": str, "elapsed": float, "stdout": None, "stderr": None, "returncode": None}``
    - ``{"status": "finished", "job_id": str, "elapsed": float, "stdout": str, "stderr": str, "returncode": int}``
    - ``{"status": "error", "job_id": str, "elapsed": float, "stdout": None, "stderr": None, "returncode": None, "error": str}``
    - ``{"status": "not_found", "job_id": str}``
    """
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return {"status": "not_found", "job_id": job_id}
    future = job["future"]
    if not future.done():
        elapsed = time.monotonic() - job["started_at"]
        # Detect zombie jobs: if the agent's subprocess timeout has passed
        # plus a 30s grace period for clipboard round-trip, the response
        # was likely lost.  Report timeout instead of running forever.
        job_timeout = job.get("timeout", 60)
        if elapsed > job_timeout + 30:
            with _jobs_lock:
                _jobs.pop(job_id, None)
            return {
                "status": "error",
                "job_id": job_id,
                "elapsed": round(elapsed, 1),
                "stdout": None,
                "stderr": None,
                "returncode": None,
                "error": f"command timed out after {job_timeout}s (response lost)",
            }
        return {
            "status": "running",
            "job_id": job_id,
            "elapsed": round(elapsed, 1),
            "stdout": None,
            "stderr": None,
            "returncode": None,
        }
    # Done — extract result and clean up
    result = future.result()
    elapsed = time.monotonic() - job["started_at"]
    with _jobs_lock:
        _jobs.pop(job_id, None)
    if result is None:
        return {
            "status": "error",
            "job_id": job_id,
            "elapsed": round(elapsed, 1),
            "stdout": None,
            "stderr": None,
            "returncode": None,
            "error": "no response from Agent",
        }
    # op_shell returns JSON with stdout/stderr/returncode
    try:
        parsed = json.loads(result)
        return {
            "status": "finished",
            "job_id": job_id,
            "elapsed": round(elapsed, 1),
            "stdout": parsed.get("stdout", ""),
            "stderr": parsed.get("stderr", ""),
            "returncode": parsed.get("returncode", -1),
        }
    except (json.JSONDecodeError, TypeError):
        return {
            "status": "finished",
            "job_id": job_id,
            "elapsed": round(elapsed, 1),
            "stdout": result,
            "stderr": "",
            "returncode": 0,
        }


def fs_read(path: str, remote_id: str | None = None) -> str | None:
    """Read a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.read", "path": path}), remote_id=remote_id)


def fs_write(path: str, content: str, remote_id: str | None = None) -> str | None:
    """Create or overwrite a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.write", "path": path, "content": content}), remote_id=remote_id
    )


def fs_list(path: str, remote_id: str | None = None) -> str | None:
    """List a directory on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.list", "path": path}), remote_id=remote_id)


def fs_delete(path: str, remote_id: str | None = None) -> str | None:
    """Delete a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.delete", "path": path}), remote_id=remote_id)


def fs_replace(path: str, old: str, new: str, remote_id: str | None = None) -> str | None:
    """Replace text in a file on the remote machine (exact-once match)."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.replace", "path": path, "old": old, "new": new}), remote_id=remote_id
    )


def fs_search(path: str, pattern: str, remote_id: str | None = None) -> str | None:
    """Search for a regex pattern in a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.search", "path": path, "pattern": pattern}), remote_id=remote_id
    )


def fs_find(path: str, pattern: str, remote_id: str | None = None) -> str | None:
    """Find files matching a glob pattern on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.find", "path": path, "pattern": pattern}), remote_id=remote_id
    )


def sysinfo(remote_id: str | None = None) -> str | None:
    """Collect system information from the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "sysinfo"}), remote_id=remote_id)


def fs_bin_read(path: str, remote_id: str | None = None) -> str | None:
    """Read a binary file on the remote machine, base64-encoded as JSON."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.bin_read", "path": path}), remote_id=remote_id)


def fs_bin_write(path: str, b64: str, remote_id: str | None = None) -> str | None:
    """Write base64-encoded content to a binary file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.bin_write", "path": path, "b64": b64}), remote_id=remote_id
    )


def upload(local_path: str, remote_path: str, remote_id: str | None = None) -> str | None:
    """Upload a local binary file to the remote machine via base64."""
    with open(local_path, "rb") as f:
        data = f.read()
    encoded = base64.b64encode(data).decode("ascii")
    return fs_bin_write(remote_path, encoded, remote_id=remote_id)


def download(remote_path: str, local_path: str, remote_id: str | None = None) -> str | None:
    """Download a binary file from the remote machine via base64."""
    response = fs_bin_read(remote_path, remote_id=remote_id)
    if response is None:
        return None
    try:
        result = json.loads(response)
        data = base64.b64decode(result["b64"])
    except (json.JSONDecodeError, KeyError, Exception) as exc:
        return f"download failed: {exc}"
    with open(local_path, "wb") as f:
        f.write(data)
    return f"downloaded {len(data)} bytes to {local_path}"

# ── Agent helpers (Controller-side, before create_server) ────────────────

def agent_login(remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "agent", "action": "login"}), remote_id=remote_id)


def agent_login_status(remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "agent", "action": "login_status"}), remote_id=remote_id)


def agent_models(remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    # Write the script to remote and run it (avoids shell quoting issues)
    script = (
        'import json, ssl, urllib.request, os\n'
        'OAUTH = None\n'
        'try:\n'
        '    import tomllib\n'
        'except ImportError:\n'
        '    try:\n'
        '        import tomli as tomllib\n'
        '    except ImportError:\n'
        '        tomllib = None\n'
        'if tomllib is not None:\n'
        '    try:\n'
        '        with open(os.path.expanduser("~/.cliptunnel/config.toml"), "rb") as f:\n'
        '            OAUTH = (tomllib.load(f).get("copilot", {}) or {}).get("oauth_token") or None\n'
        '    except Exception:\n'
        '        OAUTH = None\n'
        'if OAUTH is None:\n'
        '    OAUTH = open(".copilot_agent_token").read().strip()\n'
        'ctx = ssl.create_default_context()\n'
        'req = urllib.request.Request("https://api.github.com/copilot_internal/v2/token")\n'
        'req.add_header("Authorization", f"token {OAUTH}")\n'
        'req.add_header("Accept", "application/json")\n'
        'req.add_header("Copilot-Integration-Id", "vscode-chat")\n'
        'req.add_header("Editor-Version", "vscode/1.99.0")\n'
        'r = urllib.request.urlopen(req, timeout=10, context=ctx)\n'
        'td = json.loads(r.read().decode())\n'
        'token = td["token"]\n'
        'req = urllib.request.Request("https://api.individual.githubcopilot.com/models")\n'
        'req.add_header("Authorization", f"Bearer {token}")\n'
        'req.add_header("Editor-Version", "vscode/1.99.0")\n'
        'req.add_header("Copilot-Integration-Id", "vscode-chat")\n'
        'r = urllib.request.urlopen(req, timeout=15, context=ctx)\n'
        'resp = json.loads(r.read().decode())\n'
        'out = []\n'
        'for x in resp.get("data", []):\n'
        '    s = x.get("capabilities", {}).get("supports", {})\n'
        '    out.append({\n'
        '        "id": x.get("id", ""),\n'
        '        "name": x.get("name", ""),\n'
        '        "vendor": x.get("vendor", ""),\n'
        '        "tools": s.get("tool_calls", False),\n'
        '        "vision": s.get("vision", False),\n'
        '        "streaming": s.get("streaming", False),\n'
        '        "reasoning": s.get("reasoning_effort", None),\n'
        '        "context_window": x.get("capabilities", {}).get("limits", {}).get("max_context_window_tokens", "?"),\n'
        '        "endpoints": x.get("supported_endpoints", []),\n'
        '        "enabled": x.get("model_picker_enabled", False),\n'
        '    })\n'
        'print(json.dumps(out))\n'
    )
    fs_write("_mcp_list_models.py", script, remote_id=remote_id)
    return controller.send_command_sync(json.dumps({"op": "shell", "cmd": "python _mcp_list_models.py"}), remote_id=remote_id)


def agent_start(task: str, model: str | None = None, context: str | None = None, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    req: dict = {"op": "agent", "action": "start", "task": task}
    if model:
        req["model"] = model
    if context:
        req["system_prompt"] = context
    return controller.send_command_sync(json.dumps(req), remote_id=remote_id)


def agent_continue(session_id: str, message: str, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({
        "op": "agent", "action": "continue", "session_id": session_id, "message": message,
    }), remote_id=remote_id)


def agent_result(session_id: str, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({
        "op": "agent", "action": "result", "session_id": session_id,
    }), remote_id=remote_id)


def agent_status(session_id: str, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({
        "op": "agent", "action": "status", "session_id": session_id,
    }), remote_id=remote_id)


def agent_clear(session_id: str, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({
        "op": "agent", "action": "clear", "session_id": session_id,
    }), remote_id=remote_id)


def agent_end(session_id: str, remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({
        "op": "agent", "action": "end", "session_id": session_id,
    }), remote_id=remote_id)


def agent_list(remote_id: str | None = None) -> str | None:
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "agent", "action": "list"}), remote_id=remote_id)


# ── MCP Server ───────────────────────────────────────────────────────────────

def create_server():
    """Build the FastMCP application with every remote tool registered.

    Imports the official ``mcp`` package lazily — the only place this module
    touches it.
    """
    from mcp.server.fastmcp import FastMCP


    mcp = FastMCP("cliptunnel")

    @mcp.tool()
    def remote_shell(cmd: str, sync_timeout: float = 10.0, timeout: float = 60.0, remote_id: str | None = None, ctx: Context | None = None) -> str:
        """Execute a shell command on the remote machine (Agent side).

        Waits up to *sync_timeout* seconds (default 10) for the command to
        finish. Fast commands return JSON
        ``{"status": "finished", "job_id": null, "elapsed", "stdout", "stderr", "returncode"}``;
        slower ones return ``{"status": "running", "job_id": str}`` — poll
        with remote_shell_result.

        The *timeout* parameter (default 60s) controls how long the Agent
        waits for the subprocess to finish before killing it. Increase it
        for long-running commands.
        """
        global _client_info_captured
        if not _client_info_captured:
            _capture_client_info(ctx)
            _client_info_captured = True
        return json.dumps(shell_auto(cmd, sync_timeout=sync_timeout, timeout=timeout, remote_id=remote_id))
    @mcp.tool()
    def remote_shell_result(job_id: str) -> str:
        """Poll for the result of an async shell command started by remote_shell.

        Returns JSON with ``status`` of ``running`` (still executing),
        ``finished`` (stdout/stderr/returncode populated, job cleaned up),
        ``error``, or ``not_found`` for an unknown/completed job_id.
        """
        return json.dumps(shell_result(job_id))

    # ── Filesystem ───────────────────────────────────────────────────────────

    @mcp.tool()
    def remote_fs_read(path: str, remote_id: str | None = None) -> str:
        """Read a file on the remote machine and return JSON
        ``{"content", "lines"}``."""
        return _send(fs_read, path, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_write(path: str, content: str, remote_id: str | None = None) -> str:
        """Write content to a file on the remote machine (creates or
        overwrites, parent dirs created as needed)."""
        return _send(fs_write, path, content, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_list(path: str, remote_id: str | None = None) -> str:
        """List entries in a directory on the remote machine as a JSON array
        of ``{"name", "size", "is_dir"}``."""
        return _send(fs_list, path, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_delete(path: str, remote_id: str | None = None) -> str:
        """Delete a file on the remote machine."""
        return _send(fs_delete, path, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_replace(path: str, old: str, new: str, remote_id: str | None = None) -> str:
        """Search and replace text in a file on the remote machine
        (exact-once match: zero or multiple matches error)."""
        return _send(fs_replace, path, old, new, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_search(path: str, pattern: str, remote_id: str | None = None) -> str:
        """Search for a regex pattern in a file on the remote machine;
        returns matching lines as JSON ``[{"line", "content"}]``."""
        return _send(fs_search, path, pattern, remote_id=remote_id)

    # ── System info ───────────────────────────────────────────────────────────

    @mcp.tool()
    def remote_sysinfo(ctx: Context | None = None, remote_id: str | None = None) -> str:
        """Return system information from the remote machine: OS, hostname,
        architecture, Python version, cliptunnel-mcp version, CPU count,
        memory, disk, current user, and working directory."""
        global _client_info_captured
        if not _client_info_captured:
            _capture_client_info(ctx)
            _client_info_captured = True
        return _send(sysinfo, remote_id=remote_id)

    @mcp.tool()
    def remote_connections(ctx: Context | None = None) -> str:
        """List all connected controllers and remotes with their info, last_seen, and status.

        Returns JSON dict with two sub-dicts:
        - ``controllers``: controller_id -> {name, version, mcp_client_name, mcp_client_version, mcp_protocol_version, last_seen, last_seen_ago, status}

        status is 'alive' or 'dead'. last_seen_ago is seconds since last message.
        """
        global _client_info_captured
        if not _client_info_captured:
            _capture_client_info(ctx)
            _client_info_captured = True
        controller = _get_controller()
        if controller is None:
            return json.dumps({"controllers": {}, "remotes": {}})
        return json.dumps(controller.get_connections())

    @mcp.tool()
    def remote_discovery() -> str:
        """Broadcast an ANNOUNCE to discover remotes and other controllers.

        Triggers a fresh broadcast on the shared clipboard. Agents that
        are running will respond with their sysinfo, and other controllers
        will register this controller's presence.

        Use this when agents may have started after the initial announcement,
        or when the clipboard was busy and the ANNOUNCE was lost.

        Returns JSON ``{"status": "announced"}``.
        """
        controller = _get_controller()
        if controller is None:
            return json.dumps({"status": "error", "error": "no controller configured"})
        controller.discover()
        return json.dumps({"status": "announced"})

    @mcp.tool()
    def remote_fs_find(path: str, pattern: str, remote_id: str | None = None) -> str:
        """Find files matching a glob pattern (``**`` recurses) under a
        directory on the remote machine."""
        return _send(fs_find, path, pattern, remote_id=remote_id)

    # ── Binary transfer ──────────────────────────────────────────────────────

    @mcp.tool()
    def remote_fs_bin_read(path: str, remote_id: str | None = None) -> str:
        """Read a binary file on the remote machine; returns JSON
        ``{"path", "size", "b64"}`` with base64 content."""
        return _send(fs_bin_read, path, remote_id=remote_id)

    @mcp.tool()
    def remote_fs_bin_write(path: str, b64: str, remote_id: str | None = None) -> str:
        """Write base64-encoded content to a binary file on the remote
        machine."""
        return _send(fs_bin_write, path, b64, remote_id=remote_id)

    @mcp.tool()
    def remote_upload(local_path: str, remote_path: str, remote_id: str | None = None) -> str:
        """Upload a local binary file to the remote machine via base64 over
        the clipboard tunnel."""
        return _send(upload, local_path, remote_path, remote_id=remote_id)

    @mcp.tool()
    def remote_download(remote_path: str, local_path: str, remote_id: str | None = None) -> str:
        """Download a binary file from the remote machine to the local
        machine via base64 over the clipboard tunnel."""
        return _send(download, remote_path, local_path, remote_id=remote_id)


    # ── Agent ──────────────────────────────────────────────────────────────

    @mcp.tool()
    def remote_agent_login(remote_id: str | None = None) -> str:
        """Start GitHub OAuth Device Flow login on the remote machine.

        Returns a user_code and verification_uri. Open the verification_uri
        in a browser and enter the user_code to authorize. The remote
        machine polls for completion in the background.

        Use remote_agent_login_status to check if login completed and
        the gho_ token was saved.

        Returns:
            JSON with "user_code", "verification_uri", "status" ("polling"),
            and "expires_in".
        """
        return _send(agent_login, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_login_status(remote_id: str | None = None) -> str:
        """Check if GitHub OAuth Device Flow login completed.

        Returns:
            JSON with "status" ("done", "polling", "error", or "idle"),
            "token_saved" (bool), and "error" (str or null).
        """
        return _send(agent_login_status, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_models(remote_id: str | None = None) -> str:
        """List available Copilot models on the remote machine.

        Returns all models accessible via the Copilot API, with their
        capabilities (tools, vision, streaming, reasoning), context window
        size, and supported endpoints.

        Use this to choose a model for remote_agent_start.

        Returns:
            JSON array of model objects with id, name, vendor, capabilities,
            context_window, and endpoints fields.
        """
        return _send(agent_models, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_start(
        task: str,
        model: str | None = None,
        context: str | None = None,
        remote_id: str | None = None,
    ) -> str:
        """Start a new autonomous agent session on the remote machine.

        The agent runs on the remote machine using the GitHub Copilot API.
        It has access to shell and filesystem tools (shell, fs_read,
        fs_write, fs_replace, fs_search, fs_list, fs_find) and will
        autonomously execute the task.

        The agent runs asynchronously — this returns immediately with
        status="running". Use remote_agent_result to poll for the final
        output.

        Default model: mai-code-1.1-flash. Use remote_agent_models to see
        all options.

        Args:
            task: The task description in natural language.
            model: Optional model name (default: mai-code-1.1-flash).
            context: Optional context string injected into the agent's
                system prompt. Use this to provide project paths,
                constraints, or instructions.

        Returns:
            JSON with "session_id" and "status" ("running"). Use
            remote_agent_result to poll.
        """
        return _send(agent_start, task, model, context, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_continue(session_id: str, message: str, remote_id: str | None = None) -> str:
        """Continue an existing agent session with a new message.

        The agent retains context from previous messages in the session.
        Returns immediately with status="running".

        Args:
            session_id: The session ID returned by remote_agent_start.
            message: The new message to send to the agent.

        Returns:
            JSON with "session_id" and "status" ("running"). Use
            remote_agent_result to poll.
        """
        return _send(agent_continue, session_id, message, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_result(session_id: str, remote_id: str | None = None) -> str:
        """Poll for the result of an async agent session.

        Returns immediately with the current status. If status is "done" or
        "error", the result field contains the final output. If status is
        "running", call again later.

        Args:
            session_id: The session ID returned by remote_agent_start.

        Returns:
            JSON with "session_id", "status", "result", and "message_count".
        """
        return _send(agent_result, session_id, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_status(session_id: str, remote_id: str | None = None) -> str:
        """Get the status of an agent session.

        Args:
            session_id: The session ID to check.

        Returns:
            JSON with session_id, status, message_count, model, and
            created_at.
        """
        return _send(agent_status, session_id, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_clear(session_id: str, remote_id: str | None = None) -> str:
        """Clear the message history of an agent session (keeps the session
        alive).

        After clearing, the agent will not remember previous messages but
        the session ID remains valid for new messages.

        Args:
            session_id: The session ID to clear.

        Returns:
            Confirmation or error message.
        """
        return _send(agent_clear, session_id, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_end(session_id: str, remote_id: str | None = None) -> str:
        """End and destroy an agent session.

        Args:
            session_id: The session ID to end.

        Returns:
            Confirmation or error message.
        """
        return _send(agent_end, session_id, remote_id=remote_id)

    @mcp.tool()
    def remote_agent_list(remote_id: str | None = None) -> str:
        """List all active agent sessions on the remote machine.

        Returns:
            JSON array of session objects with session_id, status, and
            model.
        """
        return _send(agent_list, remote_id=remote_id)

    @mcp.tool()
    def remote_install_instructions() -> str:
        """Return instructions for installing and configuring the Agent on a
        remote machine, tailored to the Controller's active transport.

        Returns JSON with the transport type, connection parameters,
        environment variables, and the exact pip + cliptunnel-agent commands
        to run on the remote side.

        WARNING: Output contains bearer tokens and AES keys. Do not log
        or expose this output beyond the operator.
        """
        transport = config.get_env("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()

        if transport == "https":
            repeater_url = config.get_env("CLIPTUNNEL_REPEATER_URL", "")
            agent_token = config.get_env("CLIPTUNNEL_REPEATER_TOKEN", "")
            aes_key_raw = config.get_env("CLIPTUNNEL_AES_KEY")

            env_vars = {
                "CLIPTUNNEL_TRANSPORT": "https",
                "CLIPTUNNEL_REPEATER_URL": repeater_url,
                "CLIPTUNNEL_REPEATER_TOKEN": agent_token,
            }

            prefix_parts = [
                "CLIPTUNNEL_TRANSPORT=https",
                f"CLIPTUNNEL_REPEATER_URL={shlex.quote(repeater_url)}",
                f"CLIPTUNNEL_REPEATER_TOKEN={shlex.quote(agent_token)}",
            ]
            pip_command = "pip install cliptunnel-mcp"

            result: dict = {
                "transport": "https",
                "repeater_url": repeater_url,
                "agent_token": agent_token,
                "env_vars": env_vars,
                "pip_command": pip_command,
            }

            if aes_key_raw:
                env_vars["CLIPTUNNEL_AES_KEY"] = aes_key_raw
                result["aes_key"] = aes_key_raw
                prefix_parts.append(f"CLIPTUNNEL_AES_KEY={shlex.quote(aes_key_raw)}")

            agent_command = " ".join(prefix_parts) + " cliptunnel-agent"
            result["agent_command"] = agent_command
            return json.dumps(result)

        if transport == "firebase":
            firebase_url = config.get_env("CLIPTUNNEL_FIREBASE_URL", "")
            firebase_token = config.get_env("CLIPTUNNEL_FIREBASE_TOKEN", "")
            aes_key_raw = config.get_env("CLIPTUNNEL_AES_KEY")

            env_vars = {
                "CLIPTUNNEL_TRANSPORT": "firebase",
                "CLIPTUNNEL_FIREBASE_URL": firebase_url,
                "CLIPTUNNEL_FIREBASE_TOKEN": firebase_token,
            }

            prefix_parts = [
                "CLIPTUNNEL_TRANSPORT=firebase",
                f"CLIPTUNNEL_FIREBASE_URL={shlex.quote(firebase_url)}",
                f"CLIPTUNNEL_FIREBASE_TOKEN={shlex.quote(firebase_token)}",
            ]
            pip_command = "pip install cliptunnel-mcp"

            result = {
                "transport": "firebase",
                "firebase_url": firebase_url,
                "firebase_token": firebase_token,
                "env_vars": env_vars,
                "pip_command": pip_command,
            }

            if aes_key_raw:
                env_vars["CLIPTUNNEL_AES_KEY"] = aes_key_raw
                result["aes_key"] = aes_key_raw
                prefix_parts.append(f"CLIPTUNNEL_AES_KEY={shlex.quote(aes_key_raw)}")

            agent_command = " ".join(prefix_parts) + " cliptunnel-agent"
            result["agent_command"] = agent_command
        if transport == "websocket":
            ws_url = config.get_env("CLIPTUNNEL_WS_URL", "")
            agent_token = config.get_env("CLIPTUNNEL_WS_TOKEN", "")
            aes_key_raw = config.get_env("CLIPTUNNEL_AES_KEY")

            env_vars = {
                "CLIPTUNNEL_TRANSPORT": "websocket",
                "CLIPTUNNEL_WS_URL": ws_url,
                "CLIPTUNNEL_WS_TOKEN": agent_token,
            }

            prefix_parts = [
                "CLIPTUNNEL_TRANSPORT=websocket",
                f"CLIPTUNNEL_WS_URL={shlex.quote(ws_url)}",
                f"CLIPTUNNEL_WS_TOKEN={shlex.quote(agent_token)}",
            ]
            pip_command = "pip install cliptunnel-mcp"

            result: dict = {
                "transport": "websocket",
                "ws_url": ws_url,
                "agent_token": agent_token,
                "env_vars": env_vars,
                "pip_command": pip_command,
            }

            if aes_key_raw:
                env_vars["CLIPTUNNEL_AES_KEY"] = aes_key_raw
                result["aes_key"] = aes_key_raw
                prefix_parts.append(f"CLIPTUNNEL_AES_KEY={shlex.quote(aes_key_raw)}")

            agent_command = " ".join(prefix_parts) + " cliptunnel-agent"
            result["agent_command"] = agent_command
            return json.dumps(result)
        # clipboard (default)
        result = {
            "transport": "clipboard",
            "env_vars": {},
            "pip_command": "pip install cliptunnel-mcp",
            "agent_command": "cliptunnel-agent",
        }
        return json.dumps(result)
    return mcp


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server over stdio (``cliptunnel-mcp[server]`` entry point).

    A :class:`~cliptunnel_mcp.clipboard_transport.ClipboardTransport` is
    created automatically and wired into a :class:`Controller`, so every
    ``remote_*`` tool can reach an Agent running on the same shared
    clipboard without extra configuration.

    Logging goes to stderr only — stdout is the JSON-RPC channel.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="ClipTunnel MCP server")
    parser.add_argument(
        "--config", metavar="PATH", default=None,
        help=(
            "path to the TOML config file "
            f"(default: {config.DEFAULT_CONFIG_PATH}, "
            "overridable via CLIPTUNNEL_CONFIG)"
        ),
    )
    args = parser.parse_args()

    # Apply the --config override before anything resolves settings.
    config.set_config_path(args.config)

    from cliptunnel_mcp.transport_factory import build_transport

    transport = build_transport()

    from cliptunnel_mcp.controller import Controller

    set_controller(Controller(transport=transport))
    logger.info("Controller wired to %s transport", transport.backend_name)

    # Announce on startup so agents discover the controller immediately.
    # The mcp_client_name fields are added to the controller registry by
    # _capture_client_info on the first tool call; a second announce is
    # not needed because agents don't use those fields.
    controller = _get_controller()
    if controller is not None:
        controller._send_announce()

    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
