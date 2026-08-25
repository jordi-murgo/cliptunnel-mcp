"""External plugin system — central registry for transports, ops, tools, config, and install instructions.

Trust model: plugins run in-process with full access — no sandbox, no
isolation.  A plugin's ``register()`` callable is invoked with the singleton
:class:`ExtensionRegistry` and can register transports, agent ops, MCP tools,
config sections, and install-instruction emitters.  Plugins are discovered via
``importlib.metadata`` entry points (group ``cliptunnel_mcp.plugins``) and a
local plugin directory (``~/.cliptunnel/plugins/`` by default).

This module has zero non-stdlib top-level imports so it can be imported
safely from any other module without risk of circular dependencies.
"""
from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from typing import Callable, Iterable

__all__ = [
    "ExtensionRegistry",
    "ToolSpec",
    "load_plugins",
    "register_builtins",
    "registry",
]

_COLLISION_MSG = "plugin namespace collision: '{name}' already registered"


@dataclass(frozen=True)
class ToolSpec:
    """Specification for an MCP controller tool.

    Attributes:
        name: The MCP tool name (registered via ``@mcp.tool()``).
        description: Human-readable description shown to MCP clients.
        input_schema: JSON Schema dict describing the tool's input parameters.
        handler: Callable invoked when the tool is called.
    """

    name: str
    description: str
    input_schema: dict
    handler: Callable


class ExtensionRegistry:
    """Central registry owning registration for five extension kinds.

    Internal dicts:
        _transports: dict[str, Callable] — transport factories.
        _ops: dict[str, Callable] — agent op handlers.
        _tools: dict[str, ToolSpec] — MCP controller tools.
        _config_sections: dict[str, dict] — env-var mappings.
        _install_instructions: dict[str, Callable] — install-instruction emitters.
    """

    def __init__(self) -> None:
        self._transports: dict[str, Callable] = {}
        self._ops: dict[str, Callable] = {}
        self._tools: dict[str, ToolSpec] = {}
        self._config_sections: dict[str, dict] = {}
        self._install_instructions: dict[str, Callable] = {}

    # ── Transport registration ───────────────────────────────────────────

    def register_transport(self, name: str, factory: Callable) -> None:
        if name in self._transports:
            raise ValueError(_COLLISION_MSG.format(name=name))
        self._transports[name] = factory

    def transport_names(self) -> list[str]:
        return list(self._transports.keys())

    def get_transport_factory(self, name: str) -> Callable:
        return self._transports[name]

    # ── Agent op registration ───────────────────────────────────────────

    def register_op(self, name: str, handler: Callable) -> None:
        if name in self._ops:
            raise ValueError(_COLLISION_MSG.format(name=name))
        self._ops[name] = handler

    def op_names(self) -> list[str]:
        return list(self._ops.keys())

    def get_op_handler(self, name: str) -> Callable:
        return self._ops[name]

    # ── MCP tool registration ───────────────────────────────────────────

    def register_tool(self, name: str, tool_spec: ToolSpec) -> None:
        if name in self._tools:
            raise ValueError(_COLLISION_MSG.format(name=name))
        self._tools[name] = tool_spec

    def tool_names(self) -> list[str]:
        return list(self._tools.keys())

    def get_tool(self, name: str) -> ToolSpec:
        return self._tools[name]

    def tools(self) -> Iterable[tuple[str, ToolSpec]]:
        return self._tools.items()

    # ── Config section registration ─────────────────────────────────────

    def register_config_section(self, name: str, env_mapping: dict) -> None:
        if name in self._config_sections:
            raise ValueError(_COLLISION_MSG.format(name=name))
        self._config_sections[name] = env_mapping

    def get_config_section(self, name: str) -> dict:
        return self._config_sections[name]

    def get_config_env_mapping(self, env_var: str) -> tuple | None:
        for section in self._config_sections.values():
            if env_var in section:
                return section[env_var]
        return None

    # ── Install-instruction emitter registration ────────────────────────

    def register_install_instructions(self, transport_name: str, emitter: Callable) -> None:
        if transport_name in self._install_instructions:
            raise ValueError(_COLLISION_MSG.format(name=transport_name))
        self._install_instructions[transport_name] = emitter

    def get_install_instructions(self, transport_name: str) -> Callable:
        return self._install_instructions[transport_name]


# ── Singleton instance ────────────────────────────────────────────────────

registry = ExtensionRegistry()

# Double-load protection flag — set to True after register_builtins() runs.
_loaded: bool = False


# ── Built-in registration ──────────────────────────────────────────────────


def _make_clipboard_factory() -> Callable:
    """Return a factory closure for the clipboard transport (lazy import)."""
    def factory(config_dict: dict):
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport
        return ClipboardTransport()
    return factory


def _make_https_factory() -> Callable:
    """Return a factory closure for the https transport (lazy import)."""
    def factory(config_dict: dict):
        from cliptunnel_mcp import config
        from urllib.parse import urlparse

        repeater_url = (config.get_env("CLIPTUNNEL_REPEATER_URL") or "").strip()
        bearer_token = (config.get_env("CLIPTUNNEL_REPEATER_TOKEN") or "").strip()

        missing: list[str] = []
        if not repeater_url:
            missing.append("CLIPTUNNEL_REPEATER_URL")
        if not bearer_token:
            missing.append("CLIPTUNNEL_REPEATER_TOKEN")
        if missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=https requires: " + ", ".join(missing)
            )

        if urlparse(repeater_url).scheme.lower() != "https":
            raise ValueError(
                "CLIPTUNNEL_REPEATER_URL must use the https scheme "
                f"(got: {repeater_url!r})"
            )

        from cliptunnel_mcp.https_transport import HttpsTransport
        return HttpsTransport(
            repeater_url=repeater_url,
            bearer_token=bearer_token,
        )
    return factory


def _make_firebase_factory() -> Callable:
    """Return a factory closure for the firebase transport (lazy import)."""
    def factory(config_dict: dict):
        from cliptunnel_mcp import config
        from urllib.parse import urlparse

        database_url = (config.get_env("CLIPTUNNEL_FIREBASE_URL") or "").strip()
        auth_token = (config.get_env("CLIPTUNNEL_FIREBASE_TOKEN") or "").strip()

        fb_missing: list[str] = []
        if not database_url:
            fb_missing.append("CLIPTUNNEL_FIREBASE_URL")
        if not auth_token:
            fb_missing.append("CLIPTUNNEL_FIREBASE_TOKEN")
        if fb_missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=firebase requires: " + ", ".join(fb_missing)
            )

        if urlparse(database_url).scheme.lower() != "https":
            raise ValueError(
                "CLIPTUNNEL_FIREBASE_URL must use the https scheme "
                f"(got: {database_url!r})"
            )

        from cliptunnel_mcp.firebase_transport import FirebaseTransport
        return FirebaseTransport(
            database_url=database_url,
            auth_token=auth_token,
        )
    return factory


def _make_websocket_factory() -> Callable:
    """Return a factory closure for the websocket transport (lazy import)."""
    def factory(config_dict: dict):
        from cliptunnel_mcp import config
        from urllib.parse import urlparse

        ws_url = (config.get_env("CLIPTUNNEL_WS_URL") or "").strip()
        ws_token = (config.get_env("CLIPTUNNEL_WS_TOKEN") or "").strip()

        ws_missing: list[str] = []
        if not ws_url:
            ws_missing.append("CLIPTUNNEL_WS_URL")
        if not ws_token:
            ws_missing.append("CLIPTUNNEL_WS_TOKEN")
        if ws_missing:
            raise ValueError(
                "CLIPTUNNEL_TRANSPORT=websocket requires: " + ", ".join(ws_missing)
            )

        scheme = urlparse(ws_url).scheme.lower()
        if scheme not in ("ws", "wss"):
            raise ValueError(
                "CLIPTUNNEL_WS_URL must use the ws:// or wss:// scheme "
                f"(got: {ws_url!r})"
            )

        from cliptunnel_mcp.ws_transport import WebSocketTransport
        return WebSocketTransport(
            ws_url=ws_url,
            bearer_token=ws_token,
        )
    return factory


def _make_tool_handler(fn) -> Callable:
    """Wrap a server.py helper function into a ToolSpec handler.

    The handler receives keyword args and calls the underlying helper,
    normalizing None to an error string (same as ``server._send``).
    """
    def handler(**kwargs):
        result = fn(**kwargs)
        return result or "ERROR: no response from Agent"
    return handler


def _emit_clipboard_instructions(config_dict: dict) -> str:
    """Emit install instructions for the clipboard transport (default)."""
    result = {
        "transport": "clipboard",
        "env_vars": {},
        "pip_command": "pip install cliptunnel-mcp",
        "agent_command": "cliptunnel-agent",
    }
    return json.dumps(result)


def _emit_https_instructions(config_dict: dict) -> str:
    """Emit install instructions for the https transport."""
    from cliptunnel_mcp import config

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


def _emit_firebase_instructions(config_dict: dict) -> str:
    """Emit install instructions for the firebase transport."""
    from cliptunnel_mcp import config

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
    return json.dumps(result)


def _emit_websocket_instructions(config_dict: dict) -> str:
    """Emit install instructions for the websocket transport."""
    from cliptunnel_mcp import config

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


def register_builtins(reg: ExtensionRegistry) -> None:
    """Register all built-in transports, ops, tools, config, and install instructions.

    This function is called once at startup, before ``load_plugins()``.
    It must not be called twice on the same registry (raises ValueError on
    collision).
    """
    # ── Transports ───────────────────────────────────────────────────────
    reg.register_transport("clipboard", _make_clipboard_factory())
    reg.register_transport("https", _make_https_factory())
    reg.register_transport("firebase", _make_firebase_factory())
    reg.register_transport("websocket", _make_websocket_factory())

    # ── Agent ops ───────────────────────────────────────────────────────
    from cliptunnel_mcp import operations

    reg.register_op("shell", operations.op_shell)
    reg.register_op("fs.read", operations.op_fs_read)
    reg.register_op("fs.write", operations.op_fs_write)
    reg.register_op("fs.list", operations.op_fs_list)
    reg.register_op("fs.delete", operations.op_fs_delete)
    reg.register_op("fs.replace", operations.op_fs_replace)
    reg.register_op("fs.search", operations.op_fs_search)
    reg.register_op("fs.find", operations.op_fs_find)
    reg.register_op("fs.bin_read", operations.op_fs_bin_read)
    reg.register_op("fs.bin_write", operations.op_fs_bin_write)
    reg.register_op("sysinfo", operations.op_sysinfo)
    reg.register_op("register", operations.op_sysinfo)
    reg.register_op("agent", operations.op_agent)

    # ── MCP tools ──────────────────────────────────────────────────────
    from cliptunnel_mcp import server

    _register_server_tools(reg, server)

    # ── Config sections ─────────────────────────────────────────────────
    from cliptunnel_mcp.config import ENV_TO_FILE

    reg.register_config_section("core", dict(ENV_TO_FILE))

    # ── Install instructions ───────────────────────────────────────────
    reg.register_install_instructions("clipboard", _emit_clipboard_instructions)
    reg.register_install_instructions("https", _emit_https_instructions)
    reg.register_install_instructions("firebase", _emit_firebase_instructions)
    reg.register_install_instructions("websocket", _emit_websocket_instructions)


def _register_server_tools(reg: ExtensionRegistry, server) -> None:
    """Register all existing @mcp.tool() tools from server.py as ToolSpec entries.

    Each tool's handler wraps the existing module-level helper function.
    The input_schema is derived from the tool function's signature.
    """
    import inspect

    # Map tool name -> (helper function, description, input_schema)
    _tool_defs = [
        ("remote_shell", server.shell_auto,
         "Execute a shell command on the remote machine (Agent side).",
         {
             "type": "object",
             "properties": {
                 "cmd": {"type": "string"},
                 "sync_timeout": {"type": "number", "default": 10.0},
                 "timeout": {"type": "number", "default": 60.0},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["cmd"],
         }),
        ("remote_shell_result", server.shell_result,
         "Poll for the result of an async shell command started by remote_shell.",
         {
             "type": "object",
             "properties": {
                 "job_id": {"type": "string"},
             },
             "required": ["job_id"],
         }),
        ("remote_fs_read", server.fs_read,
         "Read a file on the remote machine and return JSON {\"content\", \"lines\"}.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path"],
         }),
        ("remote_fs_write", server.fs_write,
         "Write content to a file on the remote machine (creates or overwrites, parent dirs created as needed).",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "content": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path", "content"],
         }),
        ("remote_fs_list", server.fs_list,
         "List entries in a directory on the remote machine as a JSON array of {\"name\", \"size\", \"is_dir\"}.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path"],
         }),
        ("remote_fs_delete", server.fs_delete,
         "Delete a file on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path"],
         }),
        ("remote_fs_replace", server.fs_replace,
         "Search and replace text in a file on the remote machine (exact-once match: zero or multiple matches error).",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "old": {"type": "string"},
                 "new": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path", "old", "new"],
         }),
        ("remote_fs_search", server.fs_search,
         "Search for a regex pattern in a file on the remote machine; returns matching lines as JSON [{\"line\", \"content\"}].",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "pattern": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path", "pattern"],
         }),
        ("remote_fs_find", server.fs_find,
         "Find files matching a glob pattern (``**`` recurses) under a directory on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "pattern": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path", "pattern"],
         }),
        ("remote_fs_bin_read", server.fs_bin_read,
         "Read a binary file on the remote machine; returns JSON {\"path\", \"size\", \"b64\"} with base64 content.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path"],
         }),
        ("remote_fs_bin_write", server.fs_bin_write,
         "Write base64-encoded content to a binary file on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "path": {"type": "string"},
                 "b64": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["path", "b64"],
         }),
        ("remote_upload", server.upload,
         "Upload a local binary file to the remote machine via base64 over the clipboard tunnel.",
         {
             "type": "object",
             "properties": {
                 "local_path": {"type": "string"},
                 "remote_path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["local_path", "remote_path"],
         }),
        ("remote_download", server.download,
         "Download a binary file from the remote machine to the local machine via base64 over the clipboard tunnel.",
         {
             "type": "object",
             "properties": {
                 "remote_path": {"type": "string"},
                 "local_path": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["remote_path", "local_path"],
         }),
        ("remote_sysinfo", server.sysinfo,
         "Return system information from the remote machine: OS, hostname, architecture, Python version, cliptunnel-mcp version, CPU count, memory, disk, current user, and working directory.",
         {
             "type": "object",
             "properties": {
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": [],
         }),
        ("remote_connections", None,
         "List all connected controllers and remotes with their info, last_seen, and status.",
         {
             "type": "object",
             "properties": {},
             "required": [],
         }),
        ("remote_discovery", None,
         "Broadcast an ANNOUNCE to discover remotes and other controllers.",
         {
             "type": "object",
             "properties": {},
             "required": [],
         }),
        ("remote_agent_login", server.agent_login,
         "Start GitHub OAuth Device Flow login on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": [],
         }),
        ("remote_agent_login_status", server.agent_login_status,
         "Check if GitHub OAuth Device Flow login completed.",
         {
             "type": "object",
             "properties": {
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": [],
         }),
        ("remote_agent_models", server.agent_models,
         "List available Copilot models on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": [],
         }),
        ("remote_agent_start", server.agent_start,
         "Start a new autonomous agent session on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "task": {"type": "string"},
                 "model": {"type": ["string", "null"]},
                 "context": {"type": ["string", "null"]},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["task"],
         }),
        ("remote_agent_continue", server.agent_continue,
         "Continue an existing agent session with a new message.",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string"},
                 "message": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["session_id", "message"],
         }),
        ("remote_agent_result", server.agent_result,
         "Poll for the result of an async agent session.",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["session_id"],
         }),
        ("remote_agent_status", server.agent_status,
         "Get the status of an agent session.",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["session_id"],
         }),
        ("remote_agent_clear", server.agent_clear,
         "Clear the message history of an agent session (keeps the session alive).",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["session_id"],
         }),
        ("remote_agent_end", server.agent_end,
         "End and destroy an agent session.",
         {
             "type": "object",
             "properties": {
                 "session_id": {"type": "string"},
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": ["session_id"],
         }),
        ("remote_agent_list", server.agent_list,
         "List all active agent sessions on the remote machine.",
         {
             "type": "object",
             "properties": {
                 "remote_id": {"type": ["string", "null"]},
             },
             "required": [],
         }),
        ("remote_install_instructions", None,
         "Return instructions for installing and configuring the Agent on a remote machine, tailored to the Controller's active transport.",
         {
             "type": "object",
             "properties": {},
             "required": [],
         }),
    ]

    for name, helper, desc, schema in _tool_defs:
        if helper is not None:
            handler = _make_tool_handler(helper)
        else:
            handler = _make_special_tool_handler(name)
        reg.register_tool(name, ToolSpec(name=name, description=desc, input_schema=schema, handler=handler))


def _make_special_tool_handler(name: str) -> Callable:
    """Create a handler for tools that don't map to a single helper function."""
    if name == "remote_connections":
        def handler(**kwargs):
            import json
            from cliptunnel_mcp.server import _get_controller
            controller = _get_controller()
            if controller is None:
                return json.dumps({"controllers": {}, "remotes": {}})
            return json.dumps(controller.get_connections())
        return handler
    elif name == "remote_discovery":
        def handler(**kwargs):
            import json
            from cliptunnel_mcp.server import _get_controller
            controller = _get_controller()
            if controller is None:
                return json.dumps({"status": "error", "error": "no controller configured"})
            controller.discover()
            return json.dumps({"status": "announced"})
        return handler
    elif name == "remote_install_instructions":
        def handler(**kwargs):
            from cliptunnel_mcp import config
            transport = config.get_env("CLIPTUNNEL_TRANSPORT", "clipboard").strip().lower()
            emitter = registry.get_install_instructions(transport)
            return emitter({})
        return handler
    else:
        raise ValueError(f"unknown special tool: {name}")


# ── Plugin discovery ───────────────────────────────────────────────────────


def _discover_entry_point_plugins() -> list:
    """Return entry points for the ``cliptunnel_mcp.plugins`` group, sorted by name.

    Handles both the Python 3.10+ dict-style and older tuple-style
    ``entry_points()`` return values.
    """
    import importlib.metadata as ilm

    try:
        eps = ilm.entry_points()
    except TypeError:
        # Python <3.9 positional group argument
        return sorted(ilm.entry_points("cliptunnel_mcp.plugins"), key=lambda e: e.name)

    if isinstance(eps, dict):
        group = eps.get("cliptunnel_mcp.plugins", [])
    else:
        # SelectableGroups (3.8-3.9)
        group = eps.select(group="cliptunnel_mcp.plugins") if hasattr(eps, "select") else []
    return sorted(group, key=lambda e: e.name)


def _discover_local_dir_plugins(plugins_dir: str) -> list:
    """Return sorted (filename, full-path) pairs for .py files in *plugins_dir*.

    Returns an empty list if the directory does not exist.
    """
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return []
    files = []
    for fname in sorted(os.listdir(plugins_dir)):
        if fname.endswith(".py") and not fname.startswith("."):
            files.append((fname, os.path.join(plugins_dir, fname)))
    return files


def _load_local_plugin(path: str, name: str, reg: ExtensionRegistry) -> None:
    """Import a .py file from *path* and call its ``register(reg)`` if present."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"cliptunnel_plugin_{name}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot create spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    register_fn = getattr(mod, "register", None)
    if register_fn is None:
        return
    register_fn(reg)


def load_plugins(reg: ExtensionRegistry | None = None) -> None:
    """Discover and load external plugins into *reg* (default: module singleton).

    Loading order:
      1. Entry points in group ``cliptunnel_mcp.plugins`` (sorted by name).
      2. ``.py`` files from the local plugin directory (sorted by filename),
         default ``~/.cliptunnel/plugins/``, overridable via
         ``CLIPTUNNEL_PLUGINS_DIR``.

    Errors per plugin are logged as warnings and the plugin is skipped.
    Sets ``_loaded`` to ``True`` to prevent double-loading.
    """
    global _loaded
    if _loaded:
        return
    if reg is None:
        reg = registry

    # ── Entry-point plugins ────────────────────────────────────────────────
    for ep in _discover_entry_point_plugins():
        try:
            mod = ep.load()
            register_fn = getattr(mod, "register", None)
            if register_fn is not None:
                register_fn(reg)
        except Exception:
            import logging
            import traceback

            logging.warning("plugin '%s' failed to load: %s", ep.name, traceback.format_exc())

    # ── Local-dir plugins ──────────────────────────────────────────────────
    plugins_dir = os.environ.get(
        "CLIPTUNNEL_PLUGINS_DIR",
        os.path.join(os.path.expanduser("~"), ".cliptunnel", "plugins"),
    )
    for fname, fpath in _discover_local_dir_plugins(plugins_dir):
        try:
            _load_local_plugin(fpath, fname[:-3], reg)
        except Exception:
            import logging
            import traceback

            logging.warning("local plugin '%s' failed to load: %s", fname, traceback.format_exc())

    _loaded = True