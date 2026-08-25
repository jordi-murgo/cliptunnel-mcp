"""Test-only plugin fixture for end-to-end plugin system tests.

Registers a custom transport ("fake"), a custom op ("fake.hello"), and a
custom tool ("fake_tool") in the registry.  Used by test_plugins.py to
verify that load_plugins() discovers and loads local-dir plugins.
"""
from __future__ import annotations

from cliptunnel_mcp.plugins import ExtensionRegistry, ToolSpec


class FakeTransport:
    """Minimal in-memory transport implementing the Transport protocol."""

    def __init__(self, config: dict) -> None:
        self._config = config

    def write(self, data: bytes) -> None:
        pass

    def read(self) -> bytes:
        return b""

    def close(self) -> None:
        pass

    @property
    def revision(self) -> int:
        return 0

    def wait_for_revision(self, rev: int) -> int:
        return rev


def _fake_transport_factory(config: dict):
    """Factory returning a FakeTransport instance."""
    return FakeTransport(config)


def _fake_hello_handler(req: dict):
    """Op handler for fake.hello."""
    return ("hello from fake plugin", False)


def _fake_tool_handler(req: dict):
    """Tool handler for fake_tool."""
    return "tool output from fake plugin"


def register(reg: ExtensionRegistry) -> None:
    """Register fake transport, op, and tool in the registry."""
    reg.register_transport("fake", _fake_transport_factory)
    reg.register_op("fake.hello", _fake_hello_handler)
    reg.register_tool(
        "fake_tool",
        ToolSpec(
            name="fake_tool",
            description="A fake tool for end-to-end plugin testing.",
            input_schema={"type": "object", "properties": {}},
            handler=_fake_tool_handler,
        ),
    )