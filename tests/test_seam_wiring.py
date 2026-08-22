"""Structural tests verifying build_transport() is used at the CLI seams.

T7: agent.py and server.py main() use build_transport() instead of
direct ClipboardTransport() construction.
"""
from __future__ import annotations

import inspect
import unittest


class TestSeamWiring(unittest.TestCase):
    def test_agent_module_uses_build_transport(self) -> None:
        """agent.py imports build_transport, not ClipboardTransport() directly."""
        from cliptunnel_mcp import agent
        source = inspect.getsource(agent)
        self.assertIn("build_transport", source)
        self.assertNotIn("ClipboardTransport()", source)

    def test_server_module_uses_build_transport(self) -> None:
        """server.py main() uses build_transport, not ClipboardTransport() directly."""
        from cliptunnel_mcp import server
        source = inspect.getsource(server)
        self.assertIn("build_transport", source)
        # ClipboardTransport is still imported for the docstring reference,
        # but direct construction should be gone from main().
        # Check that main() body doesn't construct ClipboardTransport().
        main_source = inspect.getsource(server.main)
        self.assertNotIn("ClipboardTransport()", main_source)


if __name__ == "__main__":
    unittest.main()