"""Agent session management and autonomous agent loop."""
from __future__ import annotations

import json
import os
import time
import threading
import uuid
from dataclasses import dataclass, field

from cliptunnel_mcp.copilot_client import CopilotClient


# ── Tool definitions (OpenAI function calling format) ─────────────────────

TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The shell command to execute"}
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "Read a file and return its content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_write",
            "description": "Write content to a file (creates or overwrites).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "content": {"type": "string", "description": "Content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_replace",
            "description": "Search and replace text in a file. The old text must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "old": {"type": "string", "description": "Text to find (must be unique)"},
                    "new": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_search",
            "description": "Search for a regex pattern in a file. Returns matching lines with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file"},
                    "pattern": {"type": "string", "description": "Regex pattern to search for"},
                },
                "required": ["path", "pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_list",
            "description": "List entries in a directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fs_find",
            "description": "Find files matching a glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Base directory"},
                    "pattern": {"type": "string", "description": "Glob pattern (e.g. *.py)"},
                },
                "required": ["path", "pattern"],
            },
        },
    },
]


@dataclass
class AgentSession:
    """An agent session with message history and async state."""
    session_id: str
    messages: list[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    model: str = "mai-code-1.1-flash"
    # Async execution state
    status: str = "idle"  # idle, running, done, error
    result: str | None = None
    thread: threading.Thread | None = None


class SessionManager:
    """Manages agent sessions in memory."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}

    def create(self, model: str = "mai-code-1.1-flash") -> str:
        """Create a new session and return its id."""
        sid = uuid.uuid4().hex[:12]
        self._sessions[sid] = AgentSession(session_id=sid, model=model)
        return sid

    def get(self, sid: str) -> AgentSession | None:
        return self._sessions.get(sid)

    def clear(self, sid: str) -> bool:
        """Clear message history but keep the session."""
        s = self._sessions.get(sid)
        if s is None:
            return False
        s.messages = []
        return True

    def end(self, sid: str) -> bool:
        """Destroy a session."""
        return self._sessions.pop(sid, None) is not None

    def list(self) -> list[str]:
        return list(self._sessions.keys())


# ── Tool execution ─────────────────────────────────────────────────────────


def execute_tool(name: str, args: dict) -> str:
    """Execute a tool by name and return the result string."""
    # Import the existing operation handlers
    from cliptunnel_mcp.operations import (
        op_shell, op_fs_read, op_fs_write, op_fs_replace,
        op_fs_search, op_fs_list, op_fs_find,
    )

    handlers = {
        "shell": lambda r: op_shell(r),
        "fs_read": lambda r: op_fs_read(r),
        "fs_write": lambda r: op_fs_write(r),
        "fs_replace": lambda r: op_fs_replace(r),
        "fs_search": lambda r: op_fs_search(r),
        "fs_list": lambda r: op_fs_list(r),
        "fs_find": lambda r: op_fs_find(r),
    }

    handler = handlers.get(name)
    if not handler:
        return f"unknown tool: {name}"

    result, is_error = handler(args)
    if is_error:
        return f"ERROR: {result}"
    return result


# ── Agent loop ─────────────────────────────────────────────────────────────


def run_agent(
    session: AgentSession,
    user_message: str,
    client: CopilotClient,
    max_turns: int = 20,
    system_prompt: str | None = None,
) -> str:
    """Run the agent loop for a user message.

    The loop:
    1. Add user message to session
    2. Call Copilot API with tools
    3. If tool_calls in response, execute each tool and add results
    4. Repeat until no more tool_calls (final answer) or max_turns
    5. Return the final assistant text
    """
    # Add system prompt if this is the first message
    if not session.messages and system_prompt:
        session.messages.append({"role": "system", "content": system_prompt})

    session.messages.append({"role": "user", "content": user_message})

    for turn in range(max_turns):
        try:
            response = client.complete(
                messages=session.messages,
                tools=TOOL_SPECS,
                model=session.model,
            )
        except Exception as exc:
            return f"Agent error: {exc}"

        choice = response.get("choices", [{}])[0]
        msg = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")

        # Add assistant message to session
        session.messages.append(msg)

        # Check if there are tool calls
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            # No tool calls — this is the final answer
            return msg.get("content", "")

        # Execute each tool call and add results
        for tc in tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            try:
                tool_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                tool_args = {}

            tool_call_id = tc.get("id", "")
            print(f"[AGENT] tool call: {tool_name}({tool_args})", flush=True)

            result = execute_tool(tool_name, tool_args)
            print(f"[AGENT] tool result: {result[:200]}", flush=True)

            session.messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    return f"Agent stopped after {max_turns} turns without a final answer."


def _run_agent_thread(
    session: AgentSession,
    user_message: str,
    client: CopilotClient,
    system_prompt: str | None = None,
    max_turns: int = 20,
) -> None:
    """Run agent in a background thread. Updates session.status and session.result."""
    session.status = "running"
    try:
        result = run_agent(session, user_message, client, max_turns, system_prompt)
        session.result = result
        session.status = "done"
    except Exception as exc:
        session.result = f"Agent error: {exc}"
        session.status = "error"


def start_agent_async(
    session: AgentSession,
    user_message: str,
    client: CopilotClient,
    system_prompt: str | None = None,
    max_turns: int = 20,
) -> None:
    """Start an agent in a background thread (non-blocking)."""
    t = threading.Thread(
        target=_run_agent_thread,
        args=(session, user_message, client, system_prompt, max_turns),
        daemon=True,
    )
    session.thread = t
    t.start()


# ── Default system prompt ──────────────────────────────────────────────────

DEFAULT_SYSTEM_PROMPT = (
    "You are an autonomous coding agent running on a Windows machine. "
    "You have access to shell commands and filesystem operations as tools. "
    "Use them to complete the task given to you. "
    "Always explain what you are doing before calling a tool. "
    "When the task is complete, provide a summary of what you did."
)