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
``vdi_download`` → ``remote_download``. The Vulcano-specific
``vdi_agent_*`` copilot session tools are intentionally not ported. Because
the remote peer is the Agent, error strings say "Agent" where vulcano said
"VDI".

This package's core ships no clipboard transport: production callers inject
a wired Controller via :func:`set_controller` before (or while) the server
runs; until then every tool call reports an error string instead of raising.
"""
from __future__ import annotations

import base64
import concurrent.futures
import json
import logging
import sys
import threading
import time
import uuid

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

def shell_auto(cmd: str, sync_timeout: float = 10.0) -> dict:
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
        json.dumps({"op": "shell", "cmd": cmd})
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


def fs_read(path: str) -> str | None:
    """Read a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.read", "path": path}))


def fs_write(path: str, content: str) -> str | None:
    """Create or overwrite a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.write", "path": path, "content": content})
    )


def fs_list(path: str) -> str | None:
    """List a directory on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.list", "path": path}))


def fs_delete(path: str) -> str | None:
    """Delete a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.delete", "path": path}))


def fs_replace(path: str, old: str, new: str) -> str | None:
    """Replace text in a file on the remote machine (exact-once match)."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.replace", "path": path, "old": old, "new": new})
    )


def fs_search(path: str, pattern: str) -> str | None:
    """Search for a regex pattern in a file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.search", "path": path, "pattern": pattern})
    )


def fs_find(path: str, pattern: str) -> str | None:
    """Find files matching a glob pattern on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.find", "path": path, "pattern": pattern})
    )


def fs_bin_read(path: str) -> str | None:
    """Read a binary file on the remote machine, base64-encoded as JSON."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(json.dumps({"op": "fs.bin_read", "path": path}))


def fs_bin_write(path: str, b64: str) -> str | None:
    """Write base64-encoded content to a binary file on the remote machine."""
    controller = _get_controller()
    if controller is None:
        return None
    return controller.send_command_sync(
        json.dumps({"op": "fs.bin_write", "path": path, "b64": b64})
    )


def upload(local_path: str, remote_path: str) -> str | None:
    """Upload a local binary file to the remote machine via base64."""
    with open(local_path, "rb") as f:
        data = f.read()
    encoded = base64.b64encode(data).decode("ascii")
    return fs_bin_write(remote_path, encoded)


def download(remote_path: str, local_path: str) -> str | None:
    """Download a binary file from the remote machine via base64."""
    response = fs_bin_read(remote_path)
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


# ── MCP Server ───────────────────────────────────────────────────────────────

def create_server():
    """Build the FastMCP application with every remote tool registered.

    Imports the official ``mcp`` package lazily — the only place this module
    touches it.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("cliptunnel")

    # ── Shell ────────────────────────────────────────────────────────────────

    @mcp.tool()
    def remote_shell(cmd: str, sync_timeout: float = 10.0) -> str:
        """Execute a shell command on the remote machine (Agent side).

        Waits up to *sync_timeout* seconds (default 10) for the command to
        finish. Fast commands return JSON
        ``{"status": "finished", "job_id": null, "elapsed", "stdout", "stderr", "returncode"}``;
        slower ones return ``{"status": "running", "job_id": str}`` — poll
        with remote_shell_result. The Agent-side operation enforces a 60 s
        subprocess timeout.
        """
        return json.dumps(shell_auto(cmd, sync_timeout=sync_timeout))

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
    def remote_fs_read(path: str) -> str:
        """Read a file on the remote machine and return JSON
        ``{"content", "lines"}``."""
        return _send(fs_read, path)

    @mcp.tool()
    def remote_fs_write(path: str, content: str) -> str:
        """Write content to a file on the remote machine (creates or
        overwrites, parent dirs created as needed)."""
        return _send(fs_write, path, content)

    @mcp.tool()
    def remote_fs_list(path: str) -> str:
        """List entries in a directory on the remote machine as a JSON array
        of ``{"name", "size", "is_dir"}``."""
        return _send(fs_list, path)

    @mcp.tool()
    def remote_fs_delete(path: str) -> str:
        """Delete a file on the remote machine."""
        return _send(fs_delete, path)

    @mcp.tool()
    def remote_fs_replace(path: str, old: str, new: str) -> str:
        """Search and replace text in a file on the remote machine
        (exact-once match: zero or multiple matches error)."""
        return _send(fs_replace, path, old, new)

    @mcp.tool()
    def remote_fs_search(path: str, pattern: str) -> str:
        """Search for a regex pattern in a file on the remote machine;
        returns matching lines as JSON ``[{"line", "content"}]``."""
        return _send(fs_search, path, pattern)

    @mcp.tool()
    def remote_fs_find(path: str, pattern: str) -> str:
        """Find files matching a glob pattern (``**`` recurses) under a
        directory on the remote machine."""
        return _send(fs_find, path, pattern)

    # ── Binary transfer ──────────────────────────────────────────────────────

    @mcp.tool()
    def remote_fs_bin_read(path: str) -> str:
        """Read a binary file on the remote machine; returns JSON
        ``{"path", "size", "b64"}`` with base64 content."""
        return _send(fs_bin_read, path)

    @mcp.tool()
    def remote_fs_bin_write(path: str, b64: str) -> str:
        """Write base64-encoded content to a binary file on the remote
        machine."""
        return _send(fs_bin_write, path, b64)

    @mcp.tool()
    def remote_upload(local_path: str, remote_path: str) -> str:
        """Upload a local binary file to the remote machine via base64 over
        the clipboard tunnel."""
        return _send(upload, local_path, remote_path)

    @mcp.tool()
    def remote_download(remote_path: str, local_path: str) -> str:
        """Download a binary file from the remote machine to the local
        machine via base64 over the clipboard tunnel."""
        return _send(download, remote_path, local_path)

    return mcp


# ── Entry point ──────────────────────────────────────────────────────────────

def main() -> None:
    """Run the MCP server over stdio (``cliptunnel-mcp[server]`` entry point).

    Logging goes to stderr only — stdout is the JSON-RPC channel. A
    Controller must be injected via :func:`set_controller` for tools to
    reach an Agent; until then every tool call reports an error string.
    """
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    if _get_controller() is None:
        logger.warning(
            "no Controller injected — tools will report errors until "
            "set_controller() is called"
        )
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
