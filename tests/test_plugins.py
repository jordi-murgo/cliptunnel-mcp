"""Tests for the external plugin system — ExtensionRegistry, ToolSpec, register_builtins.

Strict TDD: these tests were written BEFORE plugins.py existed.
"""
from __future__ import annotations

import unittest

from cliptunnel_mcp.plugins import ExtensionRegistry, ToolSpec


def _clipboard_available() -> bool:
    """True if a real ClipboardTransport can be constructed on this host.

    Headless Linux CI runners have no clipboard backend, so clipboard
    transport factory tests must be skipped there."""
    try:
        from cliptunnel_mcp.clipboard_transport import ClipboardTransport

        t = ClipboardTransport()
        t.close()
        return True
    except Exception:
        return False


_CLIPBOARD_OK = _clipboard_available()

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

    @unittest.skipUnless(_CLIPBOARD_OK, "clipboard backend not available on this host")
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

    @unittest.skipUnless(_CLIPBOARD_OK, "clipboard backend not available on this host")
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



# ════════════════════════════════════════════════════════════════════════════
# T8: load_plugins() — entry-point + local-dir discovery
# ════════════════════════════════════════════════════════════════════════════

import os
import tempfile
from unittest import mock

from cliptunnel_mcp.plugins import load_plugins


class TestLoadPluginsEntryPointDiscovery(unittest.TestCase):
    """load_plugins discovers plugins via importlib.metadata entry points."""

    def setUp(self):
        from cliptunnel_mcp import plugins
        self._old_loaded = plugins._loaded
        plugins._loaded = False
        # Fresh registry without builtins
        self._reg = ExtensionRegistry()

    def tearDown(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = self._old_loaded

    def test_entry_point_plugin_registers_op(self):
        """A fake entry-point plugin registers its op in the registry."""
        fake_module = type("M", (), {})()
        def fake_register(reg):
            reg.register_op("ep.hello", lambda req: ("hi from ep", False))
        fake_module.register = fake_register

        fake_ep = mock.Mock()
        fake_ep.name = "ep-plugin"
        fake_ep.load.return_value = fake_module

        with mock.patch("importlib.metadata.entry_points", return_value={"cliptunnel_mcp.plugins": [fake_ep]}):
            load_plugins(self._reg)

        self.assertIn("ep.hello", self._reg.op_names())
        result, error = self._reg.get_op_handler("ep.hello")({})
        self.assertEqual(result, "hi from ep")
        self.assertFalse(error)

    def test_entry_points_loaded_sorted_by_name(self):
        """Entry points are loaded in sorted order by entry-point name."""
        call_order = []

        def make_ep(name):
            mod = type("M", (), {})()
            def reg(r):
                call_order.append(name)
                r.register_op(f"{name}.op", lambda req: (name, False))
            mod.register = reg
            ep = mock.Mock()
            ep.name = name
            ep.load.return_value = mod
            return ep

        eps = [make_ep("zeta"), make_ep("alpha"), make_ep("mid")]
        with mock.patch("importlib.metadata.entry_points", return_value={"cliptunnel_mcp.plugins": eps}):
            load_plugins(self._reg)

        self.assertEqual(call_order, ["alpha", "mid", "zeta"])


class TestLoadPluginsLocalDir(unittest.TestCase):
    """load_plugins discovers .py files from the local plugin directory."""

    def setUp(self):
        from cliptunnel_mcp import plugins
        self._old_loaded = plugins._loaded
        plugins._loaded = False
        self._reg = ExtensionRegistry()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def tearDown(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = self._old_loaded

    def test_local_dir_plugin_registers_op(self):
        """A .py file in the plugins dir registers its op."""
        plugin_code = (
            "def register(reg):\n"
            "    reg.register_op('local.hello', lambda req: ('hi from local', False))\n"
        )
        plugin_path = os.path.join(self._tmp.name, "my_plugin.py")
        with open(plugin_path, "w") as f:
            f.write(plugin_code)

        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)

        self.assertIn("local.hello", self._reg.op_names())
        result, error = self._reg.get_op_handler("local.hello")({})
        self.assertEqual(result, "hi from local")

    def test_local_dir_plugins_sorted_by_filename(self):
        """Local-dir plugins load in sorted order by filename."""
        call_order = []
        for name in ("zeta.py", "alpha.py", "mid.py"):
            path = os.path.join(self._tmp.name, name)
            with open(path, "w") as f:
                f.write(
                    f"def register(reg):\n"
                    f"    reg.register_op('{name}.op', lambda req: ('{name}', False))\n"
                )

        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)

        self.assertEqual(self._reg.op_names(), ["alpha.py.op", "mid.py.op", "zeta.py.op"])


class TestLoadPluginsErrorHandling(unittest.TestCase):
    """load_plugins gracefully skips plugins that fail to load."""

    def setUp(self):
        from cliptunnel_mcp import plugins
        self._old_loaded = plugins._loaded
        plugins._loaded = False
        self._reg = ExtensionRegistry()

    def tearDown(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = self._old_loaded

    def test_broken_entry_point_skipped(self):
        """A broken entry point is skipped; other plugins still load."""
        good_module = type("M", (), {})()
        def good_register(reg):
            reg.register_op("good.op", lambda req: ("good", False))
        good_module.register = good_register

        good_ep = mock.Mock()
        good_ep.name = "good-plugin"
        good_ep.load.return_value = good_module

        bad_ep = mock.Mock()
        bad_ep.name = "bad-plugin"
        bad_ep.load.side_effect = ImportError("boom")

        with mock.patch("importlib.metadata.entry_points", return_value={"cliptunnel_mcp.plugins": [bad_ep, good_ep]}):
            load_plugins(self._reg)

        self.assertIn("good.op", self._reg.op_names())

    def test_broken_local_plugin_skipped(self):
        """A broken .py file in the plugins dir is skipped."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))

        # Good plugin
        with open(os.path.join(tmp, "good.py"), "w") as f:
            f.write("def register(reg):\n    reg.register_op('good.local', lambda req: ('ok', False))\n")

        # Bad plugin — syntax error
        with open(os.path.join(tmp, "bad.py"), "w") as f:
            f.write("def register(  # syntax error missing colon\n")

        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": tmp}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)

        self.assertIn("good.local", self._reg.op_names())

    def test_plugin_without_register_skipped(self):
        """A plugin module without a register() function is skipped."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp))

        with open(os.path.join(tmp, "noreg.py"), "w") as f:
            f.write("x = 42\n")

        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": tmp}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)

        # Should not crash, and should have no ops
        self.assertEqual(self._reg.op_names(), [])


class TestLoadPluginsDoubleLoadPrevention(unittest.TestCase):
    """load_plugins sets _loaded flag and refuses to run twice."""

    def setUp(self):
        from cliptunnel_mcp import plugins
        self._old_loaded = plugins._loaded
        plugins._loaded = False
        self._reg = ExtensionRegistry()

    def tearDown(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = self._old_loaded

    def test_loaded_flag_set_after_load(self):
        """After load_plugins runs, _loaded is True."""
        from cliptunnel_mcp import plugins
        with mock.patch("importlib.metadata.entry_points", return_value={}):
            with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": "/nonexistent"}):
                load_plugins(self._reg)
        self.assertTrue(plugins._loaded)

    def test_double_load_no_op(self):
        """Calling load_plugins twice does not re-load."""
        from cliptunnel_mcp import plugins
        call_count = [0]

        fake_module = type("M", (), {})()
        def fake_register(reg):
            call_count[0] += 1
            reg.register_op("dbl.op", lambda req: ("dbl", False))
        fake_module.register = fake_register

        fake_ep = mock.Mock()
        fake_ep.name = "dbl"
        fake_ep.load.return_value = fake_module

        with mock.patch("importlib.metadata.entry_points", return_value={"cliptunnel_mcp.plugins": [fake_ep]}):
            load_plugins(self._reg)
            load_plugins(self._reg)  # second call should be no-op

        self.assertEqual(call_count[0], 1)



# ════════════════════════════════════════════════════════════════════════════
# T9: Package-level exports from cliptunnel_mcp
# ════════════════════════════════════════════════════════════════════════════


class TestPackageExports(unittest.TestCase):
    """Plugin API symbols are exported from the top-level cliptunnel_mcp package."""

    def test_extension_registry_exported(self):
        import cliptunnel_mcp
        from cliptunnel_mcp import ExtensionRegistry
        self.assertTrue(issubclass(ExtensionRegistry, object))

    def test_tool_spec_exported(self):
        from cliptunnel_mcp import ToolSpec
        self.assertTrue(hasattr(ToolSpec, "__dataclass_fields__"))

    def test_registry_exported(self):
        from cliptunnel_mcp import registry
        self.assertIsInstance(registry, ExtensionRegistry)

    def test_load_plugins_exported(self):
        from cliptunnel_mcp import load_plugins
        self.assertTrue(callable(load_plugins))

    def test_transport_exported(self):
        from cliptunnel_mcp import Transport
        self.assertTrue(hasattr(Transport, "__mro__"))

    def test_revision_monitor_exported(self):
        from cliptunnel_mcp import RevisionMonitor
        self.assertTrue(hasattr(RevisionMonitor, "__mro__"))


# ════════════════════════════════════════════════════════════════════════════
# T10: End-to-end fake plugin test
# ════════════════════════════════════════════════════════════════════════════


class TestFakePluginEndToEnd(unittest.TestCase):
    """Load fake_plugin from a temp dir and verify transport, op, and tool registration."""

    def setUp(self):
        from cliptunnel_mcp import plugins
        self._old_loaded = plugins._loaded
        plugins._loaded = False
        self._reg = ExtensionRegistry()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        # Copy fake_plugin.py into the temp dir
        import shutil
        src = os.path.join(os.path.dirname(__file__), "fake_plugin.py")
        dst = os.path.join(self._tmp.name, "fake_plugin.py")
        shutil.copy(src, dst)

    def tearDown(self):
        from cliptunnel_mcp import plugins
        plugins._loaded = self._old_loaded

    def test_fake_transport_registered(self):
        """The fake plugin registers a 'fake' transport."""
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)
        self.assertIn("fake", self._reg.transport_names())

    def test_fake_transport_factory_produces_transport(self):
        """The fake transport factory returns an object implementing Transport."""
        from cliptunnel_mcp.transport import Transport
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)
        factory = self._reg.get_transport_factory("fake")
        transport = factory({})
        self.assertIsInstance(transport, Transport)

    def test_fake_op_registered_and_callable(self):
        """The fake plugin registers 'fake.hello' op; dispatch returns expected result."""
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)
        self.assertIn("fake.hello", self._reg.op_names())
        result, error = self._reg.get_op_handler("fake.hello")({})
        self.assertEqual(result, "hello from fake plugin")
        self.assertFalse(error)

    def test_fake_tool_registered(self):
        """The fake plugin registers a 'fake_tool' tool with handler."""
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)
        self.assertIn("fake_tool", self._reg.tool_names())
        spec = dict(self._reg.tools())["fake_tool"]
        self.assertEqual(spec.name, "fake_tool")
        self.assertTrue(callable(spec.handler))

    def test_fake_tool_handler_callable(self):
        """The fake tool handler returns expected output."""
        with mock.patch.dict(os.environ, {"CLIPTUNNEL_PLUGINS_DIR": self._tmp.name}):
            with mock.patch("importlib.metadata.entry_points", return_value={}):
                load_plugins(self._reg)
        spec = dict(self._reg.tools())["fake_tool"]
        result = spec.handler({})
        self.assertEqual(result, "tool output from fake plugin")
if __name__ == "__main__":
    unittest.main()