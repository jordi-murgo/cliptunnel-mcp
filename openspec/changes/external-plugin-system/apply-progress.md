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

### Remaining Tasks

- T3: Refactor transport_factory.py to use registry lookup
- T4: Refactor operations.py dispatch to use registry lookup
- T5: Refactor server.py create_server() to iterate registry tools
- T6: Refactor server.py remote_install_instructions to use registry
- T7: Refactor config.py to use registry config env-mapping fallback
- T8: Implement load_plugins() discovery in plugins.py
- T9: Update __init__.py exports + pyproject.toml entry-point
- T10: Create tests/fake_plugin.py + end-to-end test
- T11: Update existing tests for error message format changes