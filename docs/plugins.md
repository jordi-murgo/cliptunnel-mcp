# Plugin System

cliptunnel-mcp supports external plugins that add new **transports**, **agent ops**, **MCP controller tools**, **config sections**, and **install-instruction emitters** — all without modifying core code.

## Trust model

Plugins run **in-process with full access**. There is no sandbox, no allowlist, no permission system. A plugin is trusted exactly as much as any pip-installed package.

- A **transport plugin** can read/write anything the transport touches (clipboard, network, files).
- An **agent op plugin** runs arbitrary code on the remote machine — same blast radius as the built-in `shell` op.
- A **controller tool plugin** runs in the MCP server process and can call any Python API.

If you wouldn't `pip install` it, don't load it as a plugin.

## Discovery

Plugins are discovered at startup via two mechanisms:

1. **Entry points** (pip-installed packages): any package declaring an entry point in the group `cliptunnel_mcp.plugins` is loaded automatically. Entry points are sorted by name.

2. **Local plugin directory**: `.py` files in `~/.cliptunnel/plugins/` (or the path in `CLIPTUNNEL_PLUGINS_DIR`) are imported at startup. Files are sorted by filename. Dotfiles (names starting with `.`) are skipped.

Loading order: core built-ins register first, then entry-point plugins (sorted by name), then local-dir plugins (sorted by filename). Plugin load failures are non-fatal — a warning is logged and the plugin is skipped.

## Writing a plugin

A plugin is a Python module (or package) that exposes a `register(registry)` function. The registry is an `ExtensionRegistry` instance:

```python
from cliptunnel_mcp.plugins import ExtensionRegistry, ToolSpec


def register(registry: ExtensionRegistry) -> None:
    """Register all plugin extensions into the registry."""
    ...
```

### Register a transport

```python
def register(registry: ExtensionRegistry) -> None:
    registry.register_transport("mytransport", _factory)


def _factory(config: dict) -> Transport:
    """Build and return a Transport + RevisionMonitor object.

    Args:
        config: dict of transport-specific settings resolved from
                env vars and the TOML config file.

    Raises:
        ValueError: if required settings are missing.
    """
    url = os.environ.get("MYTRANSPORT_URL")
    if not url:
        raise ValueError("MYTRANSPORT_URL is required for mytransport")

    return MyTransport(url=url)
```

The factory receives a config dict but built-in factories call `config.get_env()` directly — both patterns work. The returned object must satisfy the `Transport` protocol (`read() -> str`, `write(value: str) -> None`) and ideally `RevisionMonitor` (`revision` property, `wait_for_change(after, timeout) -> int`).

Optional attributes consumed by Controller/Agent:

| Attribute | Type | Purpose |
|-----------|------|---------|
| `backend_name` | `str` | Reported in sysinfo (e.g. `"mytransport"`). |
| `endpoint` | `str \| None` | Sanitized endpoint for sysinfo — **never include tokens**. |
| `close()` | `() -> None` | Idempotent shutdown. |
| `restore_user_clipboard()` | `() -> bool` | Restore the user's clipboard after an exchange (clipboard transports only). |

### Register an agent op

```python
def register(registry: ExtensionRegistry) -> None:
    registry.register_op("myop.hello", _handle_hello)


def _handle_hello(payload: dict) -> tuple[str, bool]:
    """Handle the 'myop.hello' op.

    Args:
        payload: Parsed JSON dict from the wire (the op payload).

    Returns:
        (response_str, is_error): the response text and whether it's an error.
    """
    name = payload.get("name", "world")
    return (f"hello {name}", False)
```

Agent ops run in the Agent's worker pool — handlers **must be thread-safe**. The error string for unknown ops (`"unknown op: {op}"`) is preserved by core and cannot be overridden.

### Register an MCP controller tool

```python
def register(registry: ExtensionRegistry) -> None:
    registry.register_tool("my_tool", ToolSpec(
        name="my_tool",
        description="My custom MCP tool",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"}
            },
            "required": ["query"],
        },
        handler=_handle_tool,
    ))


def _handle_tool(query: str) -> str:
    """Tool handler — called by FastMCP when the tool is invoked."""
    return f"Results for: {query}"
```

Tools registered via the registry that are not already statically registered by core are added to the FastMCP server at `create_server()` time.

### Register config sections

```python
def register(registry: ExtensionRegistry) -> None:
    registry.register_config_section("myplugin", {
        "MYTRANSPORT_URL": (("myplugin", "url"),),
        "MYTRANSPORT_TOKEN": (("myplugin", "token"),),
    })
```

This maps env vars to TOML paths so `config.get_env("MYTRANSPORT_URL")` resolves from `~/.cliptunnel/config.toml`:

```toml
[myplugin]
url = "https://my-transport.example.com"
token = "secret"
```

### Register install instructions

```python
def register(registry: ExtensionRegistry) -> None:
    registry.register_install_instructions("mytransport", _install_instructions)


def _install_instructions(config: dict) -> str:
    """Return a JSON string with installation instructions for the remote agent."""
    import json
    return json.dumps({
        "transport": "mytransport",
        "env_vars": {"MYTRANSPORT_URL": "https://my-transport.example.com"},
        "pip_command": "pip install cliptunnel-mcp-plugin-mytransport",
    })
```

When `CLIPTUNNEL_TRANSPORT=mytransport`, the `remote_install_instructions` MCP tool looks up this emitter. Unknown transports fall back to the clipboard default.

## Packaging a plugin

### pip-installable plugin

In your plugin's `pyproject.toml`:

```toml
[project]
name = "cliptunnel-mcp-plugin-mytransport"
version = "1.0.0"
dependencies = ["cliptunnel-mcp>=1.1.0"]

[project.entry-points."cliptunnel_mcp.plugins"]
mytransport = "cliptunnel_mcp_plugin_mytransport"
```

The entry point references a module with a `register(registry)` function. Install with `pip install cliptunnel-mcp-plugin-mytransport` and it auto-loads on next startup.

### Local plugin (no installation)

Create a `.py` file in `~/.cliptunnel/plugins/`:

```
~/.cliptunnel/plugins/my_plugin.py
```

```python
from cliptunnel_mcp.plugins import ExtensionRegistry, ToolSpec


def register(registry: ExtensionRegistry) -> None:
    registry.register_transport("mytransport", _factory)
    registry.register_op("myop.hello", _handle_hello)
    # ...


def _factory(config: dict):
    ...


def _handle_hello(payload: dict) -> tuple[str, bool]:
    return ("hello", False)
```

Override the directory with `CLIPTUNNEL_PLUGINS_DIR`:

```bash
export CLIPTUNNEL_PLUGINS_DIR=/path/to/my/plugins
```

## Namespace collisions

If a plugin tries to register a name that's already taken (by core or another plugin), `register_*` raises `ValueError` and the plugin fails to load. Core built-ins register first, so a plugin cannot accidentally override `clipboard`, `shell`, `fs.read`, etc.

## API reference

### `ExtensionRegistry`

| Method | Signature | Description |
|--------|-----------|-------------|
| `register_transport` | `(name: str, factory: Callable) -> None` | Register a transport factory. |
| `register_op` | `(name: str, handler: Callable) -> None` | Register an agent op handler. |
| `register_tool` | `(name: str, spec: ToolSpec) -> None` | Register an MCP controller tool. |
| `register_config_section` | `(name: str, env_mapping: dict) -> None` | Register env-var to TOML path mappings. |
| `register_install_instructions` | `(transport_name: str, emitter: Callable) -> None` | Register install instructions for a transport. |
| `transport_names` | `() -> list[str]` | List registered transport names. |
| `op_names` | `() -> list[str]` | List registered op names. |
| `tool_names` | `() -> list[str]` | List registered tool names. |
| `get_transport_factory` | `(name: str) -> Callable` | Get a transport factory by name. |
| `get_op_handler` | `(name: str) -> Callable` | Get an op handler by name. |
| `get_tool` | `(name: str) -> ToolSpec` | Get a tool spec by name. |
| `get_config_section` | `(name: str) -> dict` | Get config env-mapping by section name. |
| `get_install_instructions` | `(transport_name: str) -> Callable` | Get install emitter by transport name. |

### `ToolSpec`

Frozen dataclass:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `str` | MCP tool name. |
| `description` | `str` | Human-readable description for MCP clients. |
| `input_schema` | `dict` | JSON Schema dict for input parameters. |
| `handler` | `Callable` | Called when the tool is invoked. |

### Package exports

```python
from cliptunnel_mcp import (
    ExtensionRegistry,
    ToolSpec,
    registry,          # singleton instance
    load_plugins,      # discovery + loading
    Transport,         # protocol
    RevisionMonitor,   # protocol
)
```