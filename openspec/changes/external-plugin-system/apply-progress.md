# Apply Progress: external-plugin-system

## Batch 1: T1 + T2

### Tasks Completed

- **T1: ExtensionRegistry + ToolSpec** — Created `src/cliptunnel_mcp/plugins.py` with:
  - `ToolSpec` frozen dataclass (name, description, input_schema, handler)
  - `ExtensionRegistry` class with 5 internal dicts (transports, ops, tools, config_sections, install_instructions)
  - All register/get/names methods for each extension kind
  - `get_config_env_mapping()` for config env-var fallback lookup
  - `tools()` iterable for (name, ToolSpec) pairs
  - Module-level `registry` singleton instance
  - `_loaded` flag for double-load protection
  - Collision detection with `ValueError("plugin namespace collision: '{name}' already registered")`
  - Missing lookups raise `KeyError`
  - Insertion order preserved (Python 3.7+ dict guarantee)
  - Zero non-stdlib top-level imports

- **T2: register_builtins()** — Implemented `register_builtins(reg)`:
  - Registers all 4 transport factories (clipboard, https, firebase, websocket) as closures with lazy imports
  - Registers all 13 agent ops from operations.py (shell, fs.read, fs.write, fs.list, fs.delete, fs.replace, fs.search, fs.find, fs.bin_read, fs.bin_write, sysinfo, register, agent)
  - Registers all 27 MCP tools from server.py as ToolSpec entries with input_schema and handlers
  - Registers "core" config section with ENV_TO_FILE mapping from config.py
  - Registers 4 install-instruction emitters (clipboard, https, firebase, websocket)
  - Transport factories preserve existing env-var validation logic
  - Lazy imports inside factory closures (no heavy deps at module level)

### Files Created

- `src/cliptunnel_mcp/plugins.py` — ExtensionRegistry, ToolSpec, register_builtins
- `tests/test_plugins.py` — 41 tests covering all registry operations, collisions, lookups, and builtins

### Files Modified

None. Only new files were created — no existing source files were touched.

### Test Results

- `python -m unittest tests.test_plugins -v` → 41 tests, all pass
- `python -m unittest discover -s tests -t .` → 539 tests, 5 pre-existing failures (unrelated to this change, caused by environment config pollution and timing issues in test_server.py)

### Commits Made

- `d029f4d` — `feat: add ExtensionRegistry, ToolSpec, and register_builtins (T1+T2)`

## Batch 2: T3-T7

### Tasks Completed

- **T3: transport_factory.py refactor** — `build_transport()` uses `registry.get_transport_factory(name)(config_dict)`:
  - Replaced hardcoded if/elif chain with registry lookup
  - Removed `_ACCEPTED` set
  - Unknown transport error lists `registry.transport_names()` via sorted join
  - `_ensure_loaded()` guard calls `register_builtins(registry)` if `_loaded` flag is False
  - Guard handles pre-loaded registry gracefully (if transports already registered, just set flag)
  - Factory closures receive a config_dict and handle their own env-var validation

- **T4: operations.py dispatch refactor** — `dispatch()` uses `registry.get_op_handler(name)` for op lookup:
  - `_ensure_loaded()` calls `register_builtins(registry)` if `_loaded` flag is False
  - Hardcoded handlers dict removed; all 13 built-in ops resolve via registry
  - Error string preserved: `"unknown op: {op}"` (exact format with space after colon)
  - Custom ops registered in registry are dispatched correctly

- **T5: server.py create_server() registry tools** — `create_server()` iterates `registry.tools()` and registers plugin tools:
  - After static `@mcp.tool()` decorators, collects already-registered tool names
  - Iterates registry tools, registers any not already statically registered via `mcp.add_tool()`
  - Plugin ToolSpec entries appear in FastMCP server alongside built-in tools
  - `_ensure_registry_loaded()` guard ensures registry is populated before iteration
  - Added `asyncio` import for `asyncio.run(mcp.list_tools())`

- **T6: server.py install instructions refactor** — `remote_install_instructions()` uses `registry.get_install_instructions(transport)`:
  - Replaced hardcoded if/elif chain over transport types with registry lookup
  - Falls back to clipboard emitter for unknown transports
  - All 4 builtin transports (clipboard, https, firebase, websocket) resolve via registry emitters
  - Custom transport emitters registered in registry work transparently

- **T7: config.py plugin env-mapping fallback** — `get_env()` checks plugin-registered config sections after `ENV_TO_FILE`:
  - Lazy import `from .plugins import registry` inside `get_env` (avoids circular import)
  - `registry.get_config_env_mapping(name)` checked when env var not in `ENV_TO_FILE`
  - Plugin env vars resolve from TOML config file using the same path resolution as built-in mappings
  - Built-in `ENV_TO_FILE` mappings take precedence over plugin mappings

### Files Modified

- `src/cliptunnel_mcp/transport_factory.py` — registry lookup, `_ensure_loaded()`, removed `_ACCEPTED`
- `src/cliptunnel_mcp/operations.py` — dispatch uses `registry.get_op_handler`, `_ensure_loaded()` guard
- `src/cliptunnel_mcp/server.py` — `create_server()` iterates registry tools, `remote_install_instructions` uses registry, `_ensure_registry_loaded()`
- `src/cliptunnel_mcp/config.py` — `get_env` adds plugin config-section fallback via lazy registry import
- `tests/test_transport_factory.py` — 2 tests: unknown transport lists registry names, registry loaded on build
- `tests/test_operations.py` — 3 tests: registry dispatch, unknown op error string, all existing ops
- `tests/test_server.py` — 6 tests: plugin tool appears in server, builtin tools still registered, custom emitter via registry, unknown transport fallback, all 4 builtins, HTTPS unchanged
- `tests/test_config.py` — 5 tests: plugin env var via registry, built-in still works, env overrides config, default when neither, no circular import

### Test Results

- `python -m unittest tests.test_plugins -v` → 41 tests, all pass
- `python -m unittest tests.test_transport_factory -v` → 32 tests, all pass
- `python -m unittest tests.test_operations -v` → 33 tests, all pass
- `python -m unittest tests.test_config -v` → 27 tests, 2 pre-existing failures (environment pollution from `~/.cliptunnel/config.toml`), 5 new T7 tests pass
- `python -m unittest tests.test_server -v` → 30 tests, 3 pre-existing failures (environment config pollution + timing), all new T5/T6 tests pass
- Combined targeted suite: 133 tests, 2 pre-existing failures

### Commits Made

- `4915832` — `refactor: use registry lookup in transport_factory (T3)`
- `1bfa368` — `refactor: use registry lookup in operations dispatch (T4)`
- `a41ff9f` — `refactor: use registry lookup in transport_factory and server (T3+T5)` (T5: create_server iterates registry tools)
- `76f3190` — `refactor: use registry for install instructions in server (T6)`
- `418b755` — `refactor: add plugin env-mapping fallback in config get_env (T7)`

### Pre-existing Test Failures (not caused by this change)

- `test_config.TestGetEnv.test_default_when_neither` — `~/.cliptunnel/config.toml` pollution sets `CLIPTUNNEL_TRANSPORT=clipboard`
- `test_config.TestGetCopilotToken.test_no_token_when_section_missing` — `~/.cliptunnel/config.toml` has `copilot.oauth_token` set
- `test_server.TestRemoteConnections.test_remote_connections_populated_after_registration` — timing-dependent agent registration
- `test_server.TestRemoteInstallInstructions.test_firebase_variant` — `CLIPTUNNEL_AES_KEY` env pollution
- `test_server.TestRemoteInstallInstructions.test_https_variant` — `CLIPTUNNEL_AES_KEY` env pollution

### Remaining Tasks

- T8: Implement load_plugins() discovery in plugins.py
- T9: Update __init__.py exports + pyproject.toml entry-point
- T10: Create tests/fake_plugin.py + end-to-end test
- T11: Update existing tests for error message format changes