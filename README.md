# cliptunnel-mcp

Operate locked-down remote machines through their clipboard — now with multi-remote support and autonomous agents.

## What it does

`cliptunnel-mcp` turns a shared clipboard into a reliable control channel between machines. When a remote machine sits behind a Citrix session, a locked-down VDI, or any environment that blocks SSH, file transfer, and networking but still exposes a clipboard, ClipTunnel tunnels commands through that single slot and exposes them as [Model Context Protocol](https://modelcontextprotocol.io) tools.

**v0.5.0** introduces CT2 protocol v2 with multi-remote support, autonomous Copilot agents, and clipboard-event integration.

The package ships four layers:

- **Protocol** — CT2 wire format with 8-char hex remote IDs, broadcast routing, keepalive pings, and typed messages (command, response, error, ack, ping).
- **Endpoints** — `Controller` (operator side) with a remote registry, and multiple `Agent` instances (remote side), each with a unique hex ID. Both run background threads with ARQ retransmission, sequence-bound deduplication, and generation-safe lifecycle.
- **MCP server** — a FastMCP application with 25 tools including shell, filesystem, binary transfer, sysinfo, remote agent management, and connection discovery.
- **Clipboard transport** — backed by [clipboard-event](https://github.com/jordi-murgo/clipboard-event) for cross-platform event-driven change detection.

## Architecture

```
Operator machine                              Remote machine(s)
┌──────────────────┐     clipboard     ┌──────────────────────┐
│  MCP Client      │    (Citrix/VDI)   │  Agent (remote_id)   │
│  (Claude, Pi)    │                   │                      │
│  ┌────────────┐  │                   │  ┌────────────────┐  │
│  │ MCP Server │  │   CT2|hex|C|...   │  │ operations     │  │
│  │ (FastMCP)  │──┼───────────────────┼──│ dispatch        │  │
│  └────────────┘  │                   │  │                │  │
│  Controller      │                   │  │ copilot_client │  │
│  - registry      │   CT2|C|hex|...   │  │ agent_session  │  │
│  - keepalive     │──┼───────────────────┼──│                │  │
│  - broadcast     │                   │  └────────────────┘  │
└──────────────────┘                   └──────────────────────┘
```

The Controller broadcasts a register command on startup. Each Agent generates a random 8-hex ID, waits a random delay (0.1–4.0s), and sends back its sysinfo. The Controller maintains a registry of all connected remotes. A keepalive thread pings idle remotes every 60s and marks them dead after 120s of silence.

### Wire format

```
CT2|<from>|<to>|<seq>|<type>|<payload>
```

| Field    | Value                                                                |
|----------|----------------------------------------------------------------------|
| `CT2`    | Protocol signature + version                                         |
| `from`   | `C` (Controller) or 8-char hex (remote ID, e.g. `a1b2c3d4`)         |
| `to`     | `C` (Controller), `*` (broadcast), or 8-char hex (specific remote)  |
| `seq`    | Positive integer, monotonic per session                             |
| `type`   | `C` (command), `R` (response), `E` (error), `A` (ack), `P` (ping)   |
| `payload`| Base64-encoded UTF-8                                                 |

### Registration flow

1. **Controller startup** → broadcasts `CT2|C|*|seq|C|{"op":"register"}`
2. **Agent startup** → generates 8-hex ID → random delay 0.1–4.0s → `CT2|<hex>|C|0|R|<sysinfo>`
3. **Agent receives broadcast** → random delay 0.1–4.0s → same registration response
4. No ACK for broadcast responses

### Keepalive

- Controller pings remotes idle > 60s: `CT2|C|<hex>|seq|P|`
- Agent responds with ACK → Controller updates `last_seen`
- Remote marked `dead` if > 120s since last message/ACK

## Installation

```bash
pip install cliptunnel-mcp          # core + cliptunnel-agent binary
pip install cliptunnel-mcp[server]  # adds MCP server binary (mcp>=1.2,<2)
```

Dependencies: `clipboard-event>=0.2.0` (cross-platform clipboard change notifications).

| Binary              | Extra needed | Purpose                                      |
|---------------------|--------------|----------------------------------------------|
| `cliptunnel-agent`  | *(none)*     | Runs the Agent on the local OS clipboard.    |
| `cliptunnel-mcp`    | `[server]`   | Runs the MCP server over stdio.              |

## Quick start

### Agent (remote machine)

```bash
cliptunnel-agent
```

> **Antivirus / EDR workaround (Windows)**: unsigned `.exe` entry points may be quarantined. Use `python -m` instead:
>
> ```bash
> python -m cliptunnel_mcp.agent    # instead of cliptunnel-agent
> python -m cliptunnel_mcp.server   # instead of cliptunnel-mcp
> ```

The Agent generates a random 8-hex ID, registers with the Controller by sending its sysinfo, then watches the clipboard for commands. It uses `clipboard-event` for change detection (event-driven on Windows and Wayland, polling on macOS and X11).

### Controller + MCP server (operator machine)

Configure your MCP client (Claude Desktop, Cursor, Pi, etc.):

```json
{
  "mcpServers": {
    "cliptunnel": {
      "command": "cliptunnel-mcp",
      "args": []
    }
  }
}
```

The server broadcasts a register command on startup, discovers connected remotes, and maintains a live registry with keepalive pings.

### Controller only (no MCP)

```python
from cliptunnel_mcp.clipboard_transport import ClipboardTransport
from cliptunnel_mcp import Controller
import json

controller = Controller(transport=ClipboardTransport())

# Send to a specific remote
future = controller.send_command(json.dumps({"op": "shell", "cmd": "whoami"}), remote_id="a1b2c3d4")
result = future.result(timeout=30)

# List connected remotes
connections = controller.get_connections()
# {"a1b2c3d4": {"os": "Windows", "status": "alive", "last_seen": 1692634123.4, "last_seen_ago": 0.3, ...}}
```

### Programmatic Agent

```python
from cliptunnel_mcp.clipboard_transport import ClipboardTransport
from cliptunnel_mcp import Agent
from cliptunnel_mcp.operations import dispatch

agent = Agent(transport=ClipboardTransport(), handler=dispatch)
# Agent generates its own remote_id and registers automatically
```

## MCP tools

The server exposes **25 tools** over stdio. All tools accept an optional `remote_id` parameter to target a specific remote. If omitted, the Controller picks the first alive remote.

### Shell & filesystem

| Tool | Description |
|------|-------------|
| `remote_shell` | Execute a shell command; auto-sync (10s) then async with `job_id` polling. |
| `remote_shell_result` | Poll for the result of an async shell command. |
| `remote_fs_read` | Read a file. |
| `remote_fs_write` | Create or overwrite a file (creates parent dirs). |
| `remote_fs_list` | List directory entries. |
| `remote_fs_delete` | Delete a file. |
| `remote_fs_replace` | Search-and-replace in a file (exact-once match). |
| `remote_fs_search` | Regex search in a file. |
| `remote_fs_find` | Glob-find files under a directory. |
| `remote_fs_bin_read` | Read a binary file as base64. |
| `remote_fs_bin_write` | Write base64 content to a binary file. |

### Binary transfer

| Tool | Description |
|------|-------------|
| `remote_upload` | Upload a local file to a remote machine. |
| `remote_download` | Download a remote file to the local machine. |

### System info

| Tool | Description |
|------|-------------|
| `remote_sysinfo` | Return system info: OS, Python, CPU, memory, disk, user, shell, agent auth, clipboard backend. |

### Remote agent (Copilot)

| Tool | Description |
|------|-------------|
| `remote_agent_login` | Start GitHub OAuth device flow for Copilot authentication. |
| `remote_agent_login_status` | Poll login state (idle/polling/done/error). |
| `remote_agent_models` | List available Copilot models on the remote. |
| `remote_agent_start` | Create an autonomous agent session (async). |
| `remote_agent_continue` | Send a message to an existing session. |
| `remote_agent_result` | Poll for the async result. |
| `remote_agent_status` | Query session status. |
| `remote_agent_list` | List active agent sessions. |
| `remote_agent_clear` | Clear session message history. |
| `remote_agent_end` | Destroy a session. |

### Connections

| Tool | Description |
|------|-------------|
| `remote_connections` | List all connected remotes with sysinfo, `last_seen` (epoch), `last_seen_ago` (seconds), and `status` (alive/dead). |

## Operations

The `dispatch` handler supports these operations:

| Operation | Parameters | Returns |
|-----------|------------|---------|
| `shell` | `cmd` | JSON: `{stdout, stderr, returncode}` |
| `fs.read` | `path` | JSON: `{content, lines}` |
| `fs.write` | `path`, `content` | `wrote N bytes to PATH` |
| `fs.list` | `path` | JSON: `[{name, size, is_dir}]` |
| `fs.delete` | `path` | `deleted PATH` |
| `fs.replace` | `path`, `old`, `new` | `replaced 1 occurrence` (exact-once) |
| `fs.search` | `path`, `pattern` | JSON: `[{line, content}]` (regex) |
| `fs.find` | `path`, `pattern` | JSON: `[PATH, ...]` (glob) |
| `fs.bin_read` | `path` | JSON: `{path, size, b64}` |
| `fs.bin_write` | `path`, `b64` | `wrote N bytes to PATH` |
| `sysinfo` | — | JSON: full system info |
| `register` | — | JSON: sysinfo (alias for agent registration) |
| `agent` | `action`, ... | JSON: session management (start, continue, result, status, clear, end, list, login, login_status) |

## Remote agent

The Agent can run autonomous Copilot-powered agents on the remote machine. Each agent session:

- Uses the GitHub Copilot API with function calling (shell, fs_read, fs_write, fs_replace, fs_search, fs_list, fs_find)
- Runs asynchronously in a background thread
- Supports multi-turn conversations with `remote_agent_continue`
- Default model: `mai-code-1.1-flash`

### Authentication

```python
# Via MCP tools:
remote_agent_login()          # Returns user_code + verification_uri
# Open https://github.com/login/device, enter the code
remote_agent_login_status()   # Returns {status: "done", token_saved: true}
```

Token stored in `.copilot_agent_token` on the remote machine.

## API surface

### `Controller`

The operator-side endpoint with remote registry and keepalive.

| Method | Description |
|--------|-------------|
| `send_command(command, remote_id=None) -> Future` | Queue a command to a specific remote (or first alive). |
| `send_command_sync(command, remote_id=None) -> str \| None` | Send and block until response or timeout. |
| `get_connections() -> dict` | Return the remote registry with sysinfo, last_seen, last_seen_ago, and status. |
| `close()` | Stop background threads. Idempotent. |

### `Agent`

The remote-side endpoint with auto-registration and ping handling.

| Method | Description |
|--------|-------------|
| `close()` | Stop this agent. Idempotent. |
| `send_registration()` | Send sysinfo to the Controller. |

Constructor parameters: `transport` (required), `handler` (required), `poll_interval`, `max_workers`, `response_ack_timeout`.

### Protocol primitives

| Symbol | Description |
|--------|-------------|
| `pack(msg) -> str` | Serialize a `Message` into wire format. |
| `unpack(raw) -> Message \| None` | Parse a wire string; `None` on malformed. |
| `validate(raw, my_id) -> bool` | True if addressed to `my_id` (`C` or hex) or broadcast. |
| `generate_remote_id() -> str` | Generate a random 8-char hex ID. |
| `Message` | Dataclass: `frm`, `to`, `seq`, `mtype`, `payload`. |
| `MsgType` | Enum: `COMMAND`, `RESPONSE`, `ERROR`, `ACK`, `PING`. |
| `SeqTracker` | Per-seq dedupe state: new → processing → done. |

## Clipboard backend

ClipTunnel uses [clipboard-event](https://github.com/jordi-murgo/clipboard-event) for cross-platform clipboard access and change detection:

| Platform | Backend | Change detection | Latency |
|----------|---------|------------------|---------|
| macOS | NSPasteboard `changeCount` | Polling (50ms) | ~50ms |
| Windows | `WM_CLIPBOARD_UPDATE` | Event-driven | Sub-ms |
| Linux / Wayland | `wl-paste --watch` | Event-driven | Sub-ms |
| Linux / X11 | `xclip`/`xsel` | Polling (100ms) | ~100ms |

The `ClipboardTransport` adapts clipboard-event to the `Transport` and `RevisionMonitor` protocols. For custom setups, implement the `Transport` protocol directly.

## Platform support

| Platform | Status | Clipboard | CI |
|----------|--------|-----------|-----|
| macOS | Tested | clipboard-event (changeCount) | macOS + Linux + Windows × Python 3.10–3.14 |
| Windows | Tested | clipboard-event (WM_CLIPBOARD_UPDATE) | Same |
| Linux / Wayland | Tested | clipboard-event (wl-paste --watch) | Same |
| Linux / X11 | Core works | clipboard-event (xclip polling) | Same |

## Development

```bash
# Create a virtual environment
uv venv && source .venv/bin/activate

# Install in development mode
uv pip install -e . pytest

# Run the test suite (232 tests)
python -m pytest -q
# or
python -m unittest discover -s tests -t .

# Bare mode — no install, just PYTHONPATH
PYTHONPATH=src:. python -m pytest -q
```

The test suite uses a deterministic `ClipboardSlot` test double. No clipboard hardware needed.

## Lifecycle and coalescing semantics

- **One command at a time**: the Controller dispatches commands serially per target remote.
- **Immediate ACK**: the Agent ACKs every command before processing.
- **One response at a time**: the Agent holds one pending response; retransmits until the Controller's ACK.
- **Keepalive**: Controller pings idle remotes (>60s), marks dead (>120s).
- **Broadcast routing**: `to=*` messages are processed by all remotes with random backoff; no ACK.
- **Targeted routing**: `to=<hex>` messages are processed only by that remote; others ignore.
- **Stale message guard**: the Controller skips R/E with `seq <= min_seq`.
- **Generation-safe**: closing and restarting never strands threads.
- **Paced writes**: bounded inter-write gap prevents message loss.

## Limitations

- **Text-only clipboard**: the protocol carries UTF-8 strings. Binary files are base64-encoded.
- **Shared slot**: multiple remotes share one clipboard; the protocol serializes all traffic.
- **No encryption**: the wire format is plain base64.
- **One Controller**: the protocol supports one Controller and multiple Agents, not multiple Controllers.

## License

MIT — see [LICENSE](https://github.com/jordi-murgo/cliptunnel-mcp/blob/main/LICENSE).