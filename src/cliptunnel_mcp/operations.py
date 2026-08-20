"""Agent-side operations — structured payload handlers for the ClipTunnel protocol.

Runs on the locked-down remote machine inside the Agent's handler. Parses a
JSON payload string and dispatches to the appropriate handler based on the
``op`` field.  Each handler returns ``(output, is_error)`` where *output* is
a string and *is_error* is True on failure.

Faithful port of vulcano-helper ``vdi_operations.py`` (same op names, JSON
request/response shapes, and error strings) minus the Vulcano-specific
``agent``/copilot session op.

Zero external dependencies — stdlib only.  Python 3.10 compatible.
"""
from __future__ import annotations

import base64
import glob as glob_module
import json
import os
import re
import subprocess


# ── dispatch ────────────────────────────────────────────────────────────────

def dispatch(payload: str) -> tuple[str, bool]:
    """Parse *payload* as JSON and dispatch to the matching handler.

    Returns ``(output, is_error)``.  On invalid JSON or unknown op,
    returns an error string with ``is_error=True``.
    """
    try:
        req = json.loads(payload)
    except (json.JSONDecodeError, TypeError) as exc:
        return (f"invalid JSON: {exc}", True)

    if not isinstance(req, dict):
        return ("payload must be a JSON object", True)

    op = req.get("op")
    if not op:
        return ("missing op field", True)

    handlers = {
        "shell": op_shell,
        "fs.read": op_fs_read,
        "fs.write": op_fs_write,
        "fs.list": op_fs_list,
        "fs.delete": op_fs_delete,
        "fs.replace": op_fs_replace,
        "fs.search": op_fs_search,
        "fs.find": op_fs_find,
        "fs.bin_read": op_fs_bin_read,
        "fs.bin_write": op_fs_bin_write,
    }

    handler = handlers.get(op)
    if handler is None:
        return (f"unknown op: {op}", True)

    return handler(req)


# ── shell ────────────────────────────────────────────────────────────────────

def op_shell(req: dict) -> tuple[str, bool]:
    """Execute a shell command, return JSON with stdout, stderr, and returncode.

    Always returns a JSON string containing all three fields regardless of
    success or failure. The error flag is True only on internal failures
    (timeout, exception, missing cmd).
    """
    cmd = req.get("cmd")
    if not cmd:
        return (json.dumps({"stdout": "", "stderr": "missing 'cmd' field", "returncode": -1}), True)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return (json.dumps({"stdout": "", "stderr": "Command timed out", "returncode": -1}), True)
    except Exception as exc:
        return (json.dumps({"stdout": "", "stderr": str(exc), "returncode": -1}), True)

    return (
        json.dumps({
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }),
        result.returncode != 0,
    )


# ── fs.read ──────────────────────────────────────────────────────────────────

def op_fs_read(req: dict) -> tuple[str, bool]:
    """Read a file, return content + line count as JSON."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    if not os.path.isfile(path):
        return (f"file not found: {path}", True)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    if content:
        lines = content.count("\n") + (0 if content.endswith("\n") else 1)
    else:
        lines = 0
    return (json.dumps({"content": content, "lines": lines}), False)


# ── fs.write ─────────────────────────────────────────────────────────────────

def op_fs_write(req: dict) -> tuple[str, bool]:
    """Create or overwrite a file, creating parent dirs as needed."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    content = req.get("content", "")
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return (f"wrote {len(content)} bytes to {path}", False)


# ── fs.list ──────────────────────────────────────────────────────────────────

def op_fs_list(req: dict) -> tuple[str, bool]:
    """List directory entries as JSON array of {name, size, is_dir}."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    if not os.path.isdir(path):
        return (f"directory not found: {path}", True)
    entries = []
    for name in sorted(os.listdir(path)):
        full = os.path.join(path, name)
        entries.append({
            "name": name,
            "size": os.path.getsize(full) if os.path.isfile(full) else 0,
            "is_dir": os.path.isdir(full),
        })
    return (json.dumps(entries), False)


# ── fs.delete ────────────────────────────────────────────────────────────────

def op_fs_delete(req: dict) -> tuple[str, bool]:
    """Delete a file."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    if not os.path.exists(path):
        return (f"file not found: {path}", True)
    os.remove(path)
    return (f"deleted {path}", False)


# ── fs.replace ───────────────────────────────────────────────────────────────

def op_fs_replace(req: dict) -> tuple[str, bool]:
    """Replace *old* with *new* in a file — exactly once or error."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    old = req.get("old")
    new = req.get("new")
    if old is None:
        return ("missing 'old' field", True)
    if new is None:
        return ("missing 'new' field", True)
    if not os.path.isfile(path):
        return (f"file not found: {path}", True)
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    count = content.count(old)
    if count == 0:
        return ("old text not found", True)
    if count > 1:
        return (f"old text matches {count} times, expected exactly 1", True)
    content = content.replace(old, new)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return (f"replaced 1 occurrence in {path}", False)


# ── fs.search ────────────────────────────────────────────────────────────────

def op_fs_search(req: dict) -> tuple[str, bool]:
    """Search for *pattern* (regex) in a file, return matches as JSON."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    pattern = req.get("pattern")
    if not pattern:
        return ("missing 'pattern' field", True)
    if not os.path.isfile(path):
        return (f"file not found: {path}", True)
    matches = []
    with open(path, "r", encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            if re.search(pattern, line):
                matches.append({"line": i, "content": line.rstrip("\n\r")})
    return (json.dumps(matches), False)


# ── fs.find ──────────────────────────────────────────────────────────────────

def op_fs_find(req: dict) -> tuple[str, bool]:
    """Glob-find files under *path* matching *pattern*."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    pattern = req.get("pattern")
    if not pattern:
        return ("missing 'pattern' field", True)
    if not os.path.isdir(path):
        return (f"directory not found: {path}", True)
    full_pattern = os.path.join(path, pattern)
    results = sorted(glob_module.glob(full_pattern, recursive=True))
    return (json.dumps(results), False)

# ── fs.bin_read ──────────────────────────────────────────────────────────────

def op_fs_bin_read(req: dict) -> tuple[str, bool]:
    """Read a file as binary, return base64-encoded content as JSON."""
    path = req.get("path")
    if not path:
        return ("missing 'path' field", True)
    if not os.path.isfile(path):
        return (f"file not found: {path}", True)
    with open(path, "rb") as f:
        data = f.read()
    encoded = base64.b64encode(data).decode("ascii")
    return (json.dumps({"path": path, "size": len(data), "b64": encoded}), False)


# ── fs.bin_write ─────────────────────────────────────────────────────────────

def op_fs_bin_write(req: dict) -> tuple[str, bool]:
    """Write base64-encoded content to a file as binary."""
    path = req.get("path")
    b64 = req.get("b64")
    if not path:
        return ("missing 'path' field", True)
    if b64 is None:
        return ("missing 'b64' field", True)
    try:
        data = base64.b64decode(b64, validate=True)
    except Exception as exc:
        return (f"invalid base64: {exc}", True)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return (f"wrote {len(data)} bytes to {path}", False)
