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
        "sysinfo": op_sysinfo,
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


# ── sysinfo ──────────────────────────────────────────────────────────────────

def op_sysinfo(req: dict) -> tuple[str, bool]:
    """Return system information as JSON.

    Includes OS, hostname, architecture, Python version, cliptunnel-mcp
    version, CPU count, memory, disk, current user, working directory,
    and environment details. Works on Windows, macOS, and Linux.
    """
    import platform
    import shutil
    import socket
    import sys

    info: dict = {}

    # ── OS ───────────────────────────────────────────────────────────
    info["os"] = platform.system()
    info["os_release"] = platform.release()
    info["os_version"] = platform.version()
    info["hostname"] = socket.gethostname()
    info["arch"] = platform.machine()
    info["processor"] = platform.processor() or "unknown"

    # ── Python ───────────────────────────────────────────────────────
    info["python_version"] = sys.version
    info["python_executable"] = sys.executable
    info["python_implementation"] = platform.python_implementation()

    # ── cliptunnel-mcp ──────────────────────────────────────────────
    try:
        from importlib.metadata import version as _pkg_version
        info["cliptunnel_mcp_version"] = _pkg_version("cliptunnel-mcp")
    except Exception:
        try:
            from cliptunnel_mcp import __version__
            info["cliptunnel_mcp_version"] = __version__
        except Exception:
            info["cliptunnel_mcp_version"] = "unknown"

    # ── User & environment ──────────────────────────────────────────
    info["user"] = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    info["cwd"] = os.getcwd()
    info["shell"] = os.environ.get("SHELL", "")
    info["home"] = os.path.expanduser("~")

    # ── CPU ─────────────────────────────────────────────────────────
    info["cpu_count"] = os.cpu_count() or 0

    # ── Memory ──────────────────────────────────────────────────────
    _add_memory_info(info)

    # ── Disk ────────────────────────────────────────────────────────
    _add_disk_info(info)

    return (json.dumps(info, indent=2, default=str), False)


def _add_memory_info(info: dict) -> None:
    """Add memory info, platform-specific."""
    import platform

    system = platform.system()
    if system == "Windows":
        try:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            info["mem_total"] = stat.ullTotalPhys
            info["mem_available"] = stat.ullAvailPhys
            info["mem_percent_used"] = stat.dwMemoryLoad
        except Exception:
            pass
    else:
        try:
            result = subprocess.run(
            ["vm_stat"] if system == "Darwin" else ["free", "-b"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if result.returncode == 0:
                if system == "Darwin":
                    _parse_darwin_vm_stat(info, result.stdout)
                else:
                    _parse_linux_free(info, result.stdout)
        except Exception:
            pass


def _parse_darwin_vm_stat(info: dict, output: str) -> None:
    """Parse macOS vm_stat output."""
    import re

    page_size = 4096
    total_match = re.search(r"page size of (\d+) bytes", output)
    if total_match:
        page_size = int(total_match.group(1))

    free_match = re.search(r"Pages free:\s+(\d+)", output)
    active_match = re.search(r"Pages active:\s+(\d+)", output)
    inactive_match = re.search(r"Pages inactive:\s+(\d+)", output)
    wired_match = re.search(r"Pages wired down:\s+(\d+)", output)
    spec_match = re.search(r"Pages occupied by compressor:\s+(\d+)", output)

    free_pages = int(free_match.group(1)) if free_match else 0
    active_pages = int(active_match.group(1)) if active_match else 0
    inactive_pages = int(inactive_match.group(1)) if inactive_match else 0
    wired_pages = int(wired_match.group(1)) if wired_match else 0
    spec_pages = int(spec_match.group(1)) if spec_match else 0

    used_pages = active_pages + wired_pages
    total_pages = free_pages + active_pages + inactive_pages + wired_pages + spec_pages

    info["mem_total"] = total_pages * page_size
    info["mem_available"] = (free_pages + inactive_pages) * page_size
    if total_pages > 0:
        info["mem_percent_used"] = round(used_pages * 100 / total_pages, 1)


def _parse_linux_free(info: dict, output: str) -> None:
    """Parse Linux free -b output."""
    lines = output.strip().split("\n")
    if len(lines) >= 2:
        parts = lines[1].split()
        if len(parts) >= 7:
            info["mem_total"] = int(parts[1])
            info["mem_available"] = int(parts[6])
            if int(parts[1]) > 0:
                info["mem_percent_used"] = round(
                    (int(parts[2]) * 100 / int(parts[1])), 1
                )


def _add_disk_info(info: dict) -> None:
    """Add disk usage for the current working directory."""
    import platform

    system = platform.system()
    try:
        if system == "Windows":
            import ctypes

            free_bytes = ctypes.c_ulonglong(0)
            total_bytes = ctypes.c_ulonglong(0)
            available = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                ctypes.c_wchar_p(os.getcwd()),
                ctypes.byref(free_bytes),
                ctypes.byref(total_bytes),
                ctypes.byref(available),
            )
            info["disk_total"] = total_bytes.value
            info["disk_free"] = free_bytes.value
        else:
            stat = os.statvfs(os.getcwd())
            info["disk_total"] = stat.f_blocks * stat.f_frsize
            info["disk_free"] = stat.f_bavail * stat.f_frsize
    except Exception:
        pass
