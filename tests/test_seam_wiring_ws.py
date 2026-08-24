# tests/test_seam_wiring_ws.py
import inspect
import unittest

from cliptunnel_mcp import agent, server


class TestSeamWiringWebSocket(unittest.TestCase):
    def test_agent_uses_build_transport(self) -> None:
        source = inspect.getsource(agent)
        self.assertIn("build_transport", source)

    def test_server_uses_build_transport(self) -> None:
        source = inspect.getsource(server)
        self.assertIn("build_transport", source)


if __name__ == "__main__":
    unittest.main()