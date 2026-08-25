"""Tests for the external plugin system — ExtensionRegistry, ToolSpec, register_builtins.

Strict TDD: these tests were written BEFORE plugins.py existed.
"""
from __future__ import annotations

import unittest

from cliptunnel_mcp.plugins import ExtensionRegistry, ToolSpec


# ════════════════════════════════════════════════════════════════════════════
# T1: ExtensionRegistry + ToolSpec
# ════════════════════════════════════════════════════════════════════════════


class TestExtensionRegistryCreation(unittest.TestCase):
    def test_empty_registry_has_no_transports(self):
        reg = ExtensionRegistry()
        self.assertEqual(reg.transport_names(), [])

    def test_empty_registry_has_no_ops(self):
        reg = ExtensionRegistry()
        self.assertEqual(reg.op_names(), [])

    def test_empty_registry_has_no_tools(self):
        reg = ExtensionRegistry()
        self.assertEqual(reg.tool_names(), [])


class TestExtensionRegistryRegistration(unittest.TestCase):
    def setUp(self):
        self.reg = ExtensionRegistry()

    def test_register_transport(self):
        factory = lambda cfg: None
        self.reg.register_transport("test-t", factory)
        self.assertIn("test-t", self.reg.transport_names())
        self.assertIs(self.reg.get_transport_factory("test-t"), factory)

    def test_register_op(self):
        handler = lambda req: ("ok", False)
        self.reg.register_op("test.op", handler)
        self.assertIn("test.op", self.reg.op_names())
        self.assertIs(self.reg.get_op_handler("test.op"), handler)

    def test_register_tool(self):
        spec = ToolSpec(name="test_tool", description="d", input_schema={}, handler=lambda: "")
        self.reg.register_tool("test_tool", spec)
        self.assertIn("test_tool", self.reg.tool_names())
        self.assertIs(self.reg.get_tool("test_tool"), spec)

    def test_register_config_section(self):
        mapping = {"CLIPTUNNEL_TEST_URL": (("plugins", "test"), "url")}
        self.reg.register_config_section("test", mapping)
        self.assertEqual(self.reg.get_config_section("test"), mapping)

    def test_register_install_instructions(self):
        emitter = lambda cfg: "{}"
        self.reg.register_install_instructions("test-t", emitter)
        self.assertIs(self.reg.get_install_instructions("test-t"), emitter)


class TestExtensionRegistryCollision(unittest.TestCase):
    def setUp(self):
        self.reg = ExtensionRegistry()
        self.factory = lambda cfg: None
        self.handler = lambda req: ("ok", False)
        self.spec = ToolSpec(name="t", description="d", input_schema={}, handler=lambda: "")
        self.mapping = {"X": (("plugins", "t"), "k")}
        self.emitter = lambda cfg: "{}"

    def test_collision_transport(self):
        self.reg.register_transport("dup", self.factory)
        with self.assertRaises(ValueError) as ctx:
            self.reg.register_transport("dup", self.factory)
        self.assertIn("plugin namespace collision", str(ctx.exception))
        self.assertIn("'dup'", str(ctx.exception))

    def test_collision_op(self):
        self.reg.register_op("dup", self.handler)
        with self.assertRaises(ValueError) as ctx:
            self.reg.register_op("dup", self.handler)
        self.assertIn("plugin namespace collision", str(ctx.exception))

    def test_collision_tool(self):
        self.reg.register_tool("dup", self.spec)
        with self.assertRaises(ValueError) as ctx:
            self.reg.register_tool("dup", self.spec)
        self.assertIn("plugin namespace collision", str(ctx.exception))

    def test_collision_config(self):
        self.reg.register_config_section("dup", self.mapping)
        with self.assertRaises(ValueError) as ctx:
            self.reg.register_config_section("dup", self.mapping)
        self.assertIn("plugin namespace collision", str(ctx.exception))

    def test_collision_install_instructions(self):
        self.reg.register_install_instructions("dup", self.emitter)
        with self.assertRaises(ValueError) as ctx:
            self.reg.register_install_instructions("dup", self.emitter)
        self.assertIn("plugin namespace collision", str(ctx.exception))


class TestExtensionRegistryMissingLookup(unittest.TestCase):
    def setUp(self):
        self.reg = ExtensionRegistry()

    def test_missing_transport_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.get_transport_factory("nope")

    def test_missing_op_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.get_op_handler("nope")

    def test_missing_tool_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.get_tool("nope")

    def test_missing_config_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.get_config_section("nope")

    def test_missing_install_instructions_raises_key_error(self):
        with self.assertRaises(KeyError):
            self.reg.get_install_instructions("nope")


class TestConfigEnvMapping(unittest.TestCase):
    def test_get_config_env_mapping_found(self):
        reg = ExtensionRegistry()
        mapping = {"CLIPTUNNEL_TEST_URL": (("plugins", "test"), "url")}
        reg.register_config_section("test", mapping)
        result = reg.get_config_env_mapping("CLIPTUNNEL_TEST_URL")
        self.assertEqual(result, (("plugins", "test"), "url"))

    def test_get_config_env_mapping_not_found(self):
        reg = ExtensionRegistry()
        self.assertIsNone(reg.get_config_env_mapping("NONEXISTENT"))

    def test_get_config_env_mapping_multiple_sections(self):
        reg = ExtensionRegistry()
        reg.register_config_section("a", {"CLIPTUNNEL_A": (("plugins", "a"), "k")})
        reg.register_config_section("b", {"CLIPTUNNEL_B": (("plugins", "b"), "k")})
        self.assertEqual(reg.get_config_env_mapping("CLIPTUNNEL_B"), (("plugins", "b"), "k"))


class TestToolSpec(unittest.TestCase):
    def test_tool_spec_fields(self):
        h = lambda **kw: "result"
        spec = ToolSpec(name="t", description="desc", input_schema={"type": "object"}, handler=h)
        self.assertEqual(spec.name, "t")
        self.assertEqual(spec.description, "desc")
        self.assertEqual(spec.input_schema, {"type": "object"})
        self.assertIs(spec.handler, h)

    def test_tool_spec_frozen(self):
        spec = ToolSpec(name="t", description="d", input_schema={}, handler=lambda: "")
        with self.assertRaises(AttributeError):
            spec.name = "changed"


class TestRegistryToolsIteration(unittest.TestCase):
    def test_tools_returns_iterable_of_pairs(self):
        reg = ExtensionRegistry()
        spec1 = ToolSpec(name="t1", description="d", input_schema={}, handler=lambda: "")
        spec2 = ToolSpec(name="t2", description="d", input_schema={}, handler=lambda: "")
        reg.register_tool("t1", spec1)
        reg.register_tool("t2", spec2)
        names = [name for name, _ in reg.tools()]
        self.assertEqual(set(names), {"t1", "t2"})


# ── TRIANGULATE ─────────────────────────────────────────────────────────────


class TestRegistryTransportNamesOrderPreserved(unittest.TestCase):
    def test_transport_names_preserves_insertion_order(self):
        reg = ExtensionRegistry()
        reg.register_transport("zebra", lambda cfg: None)
        reg.register_transport("apple", lambda cfg: None)
        reg.register_transport("mango", lambda cfg: None)
        self.assertEqual(reg.transport_names(), ["zebra", "apple", "mango"])


class TestRegistryOpsOrderPreserved(unittest.TestCase):
    def test_op_names_preserves_insertion_order(self):
        reg = ExtensionRegistry()
        reg.register_op("z.op", lambda req: ("", False))
        reg.register_op("a.op", lambda req: ("", False))
        self.assertEqual(reg.op_names(), ["z.op", "a.op"])


class TestRegisterTransportAcceptsCallableDictParam(unittest.TestCase):
    def test_factory_receives_config_dict(self):
        received = {}
        def factory(cfg):
            received.update(cfg)
            return None
        reg = ExtensionRegistry()
        reg.register_transport("test", factory)
        reg.get_transport_factory("test")({"key": "val"})
        self.assertEqual(received, {"key": "val"})


class TestRegistrySingleton(unittest.TestCase):
    def test_singleton_exists(self):
        from cliptunnel_mcp.plugins import registry
        self.assertIsInstance(registry, ExtensionRegistry)


class TestRegistryLoadedFlag(unittest.TestCase):
    def test_loaded_flag_starts_false(self):
        from cliptunnel_mcp import plugins
        # _loaded should exist; reset to False for test isolation
        plugins._loaded = False
        self.assertFalse(plugins._loaded)

    def test_loaded_flag_set_true(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = True
        self.assertTrue(plugins._loaded)
        plugins._loaded = False  # cleanup


# ════════════════════════════════════════════════════════════════════════════
# T2: register_builtins()
# ════════════════════════════════════════════════════════════════════════════

from cliptunnel_mcp.plugins import register_builtins


class TestRegisterBuiltins(unittest.TestCase):
    def setUp(self):
        self.reg = ExtensionRegistry()
        register_builtins(self.reg)

    def test_builtin_transports_registered(self):
        names = self.reg.transport_names()
        for t in ("clipboard", "https", "firebase", "websocket"):
            self.assertIn(t, names)

    def test_builtin_ops_registered(self):
        names = self.reg.op_names()
        for op in ("shell", "fs.read", "fs.write", "fs.list", "fs.delete",
                   "fs.replace", "fs.search", "fs.find", "fs.bin_read",
                   "fs.bin_write", "sysinfo", "register", "agent"):
            self.assertIn(op, names)

    def test_builtin_tools_registered(self):
        names = self.reg.tool_names()
        for tool in ("remote_shell", "remote_fs_read", "remote_sysinfo",
                     "remote_install_instructions"):
            self.assertIn(tool, names)

    def test_core_config_section_registered(self):
        section = self.reg.get_config_section("core")
        self.assertIn("CLIPTUNNEL_TRANSPORT", section)

    def test_install_instructions_registered(self):
        for t in ("clipboard", "https", "firebase", "websocket"):
            self.assertIsNotNone(self.reg.get_install_instructions(t))

    def test_clipboard_factory_returns_transport(self):
        from cliptunnel_mcp.transport import Transport
        factory = self.reg.get_transport_factory("clipboard")
        t = factory({})
        self.assertIsInstance(t, Transport)

    def test_https_factory_validates_missing_url(self):
        import os
        factory = self.reg.get_transport_factory("https")
        old_url = os.environ.pop("CLIPTUNNEL_REPEATER_URL", None)
        old_tok = os.environ.pop("CLIPTUNNEL_REPEATER_TOKEN", None)
        try:
            with self.assertRaises(ValueError) as ctx:
                factory({})
            self.assertIn("CLIPTUNNEL_REPEATER_URL", str(ctx.exception))
        finally:
            if old_url: os.environ["CLIPTUNNEL_REPEATER_URL"] = old_url
            if old_tok: os.environ["CLIPTUNNEL_REPEATER_TOKEN"] = old_tok


class TestRegisterBuiltinsIdempotentCollision(unittest.TestCase):
    def test_double_registration_raises_collision(self):
        reg = ExtensionRegistry()
        register_builtins(reg)
        with self.assertRaises(ValueError):
            register_builtins(reg)


class TestBuiltinTransportFactoriesProduceCorrectTypes(unittest.TestCase):
    def setUp(self):
        self.reg = ExtensionRegistry()
        register_builtins(self.reg)

    def test_clipboard_factory_produces_clipboard_transport(self):
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport
        t = self.reg.get_transport_factory("clipboard")({})
        self.assertIsInstance(t, ClipboardTransport)


class TestBuiltinToolCount(unittest.TestCase):
    def test_tool_count_matches_expected_tools(self):
        from tests.test_server import EXPECTED_TOOLS
        reg = ExtensionRegistry()
        register_builtins(reg)
        self.assertEqual(set(reg.tool_names()), EXPECTED_TOOLS)


class TestBuiltinOpCount(unittest.TestCase):
    def test_all_thirteen_ops_registered(self):
        reg = ExtensionRegistry()
        register_builtins(reg)
        expected_ops = {
            "shell", "fs.read", "fs.write", "fs.list", "fs.delete",
            "fs.replace", "fs.search", "fs.find", "fs.bin_read",
            "fs.bin_write", "sysinfo", "register", "agent",
        }
        self.assertEqual(set(reg.op_names()), expected_ops)


if __name__ == "__main__":
    unittest.main()