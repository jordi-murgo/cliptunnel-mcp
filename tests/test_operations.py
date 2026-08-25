"""Unit tests for cliptunnel_mcp.operations — stdlib unittest, zero deps.

Ported from vulcano-helper tests/test_operations.py (minus the Vulcano-specific
agent/copilot op) with the same JSON request/response shapes and error strings.

Tests run on macOS (the test machine).  Uses tempfile.TemporaryDirectory
for all filesystem tests so nothing touches the real filesystem, and never
touches the clipboard — dispatch is a pure function.
"""
import json
import os
import tempfile
import unittest

from cliptunnel_mcp.operations import dispatch


class TestDispatch(unittest.TestCase):
    """Dispatch-level tests: JSON parsing, unknown op, missing op."""

    def test_malformed_json_returns_error(self):
        out, err = dispatch("not json at all")
        self.assertTrue(err)
        self.assertIn("invalid JSON", out)

    def test_missing_op_returns_error(self):
        out, err = dispatch(json.dumps({"cmd": "ls"}))
        self.assertTrue(err)
        self.assertIn("missing op", out)

    def test_unknown_op_returns_error(self):
        out, err = dispatch(json.dumps({"op": "nonexistent"}))
        self.assertTrue(err)
        self.assertIn("unknown op", out)


class TestShell(unittest.TestCase):
    """shell operation."""

    def test_shell_returns_stdout(self):
        out, err = dispatch(json.dumps({"op": "shell", "cmd": "echo hello"}))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertEqual(data["stdout"].strip(), "hello")
        self.assertEqual(data["stderr"], "")
        self.assertEqual(data["returncode"], 0)

    def test_shell_returns_stderr_on_error(self):
        out, err = dispatch(json.dumps({"op": "shell", "cmd": "ls /nonexistent_dir_12345"}))
        self.assertTrue(err)
        data = json.loads(out)
        # ls writes to stderr when the dir doesn't exist
        self.assertTrue(len(data["stderr"]) > 0)
        self.assertNotEqual(data["returncode"], 0)

    def test_shell_missing_cmd_errors(self):
        out, err = dispatch(json.dumps({"op": "shell"}))
        self.assertTrue(err)
        data = json.loads(out)
        self.assertEqual(data["stderr"], "missing 'cmd' field")
        self.assertEqual(data["returncode"], -1)


class TestFsRead(unittest.TestCase):
    """fs.read operation."""

    def test_read_file_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "f.txt")
            with open(p, "w") as f:
                f.write("line1\nline2\nline3\n")
            out, err = dispatch(json.dumps({"op": "fs.read", "path": p}))
            self.assertFalse(err)
            data = json.loads(out)
            self.assertEqual(data["content"], "line1\nline2\nline3\n")
            self.assertEqual(data["lines"], 3)

    def test_read_missing_file_errors(self):
        out, err = dispatch(json.dumps({"op": "fs.read", "path": "/no/such/file_xyz.txt"}))
        self.assertTrue(err)
        self.assertIn("file not found", out)


class TestFsWrite(unittest.TestCase):
    """fs.write operation."""

    def test_write_creates_new_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "new.txt")
            out, err = dispatch(json.dumps({"op": "fs.write", "path": p, "content": "hello"}))
            self.assertFalse(err)
            with open(p, "r") as f:
                self.assertEqual(f.read(), "hello")

    def test_write_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "existing.txt")
            with open(p, "w") as f:
                f.write("old")
            out, err = dispatch(json.dumps({"op": "fs.write", "path": p, "content": "new"}))
            self.assertFalse(err)
            with open(p, "r") as f:
                self.assertEqual(f.read(), "new")

    def test_write_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "deep", "file.txt")
            out, err = dispatch(json.dumps({"op": "fs.write", "path": p, "content": "nested"}))
            self.assertFalse(err)
            with open(p, "r") as f:
                self.assertEqual(f.read(), "nested")


class TestFsList(unittest.TestCase):
    """fs.list operation."""

    def test_list_returns_entries(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "subdir"))
            with open(os.path.join(d, "a.txt"), "w") as f:
                f.write("aa")
            with open(os.path.join(d, "b.txt"), "w") as f:
                f.write("bbb")
            out, err = dispatch(json.dumps({"op": "fs.list", "path": d}))
            self.assertFalse(err)
            entries = json.loads(out)
            names = [e["name"] for e in entries]
            self.assertIn("a.txt", names)
            self.assertIn("b.txt", names)
            self.assertIn("subdir", names)
            for e in entries:
                if e["name"] == "a.txt":
                    self.assertEqual(e["size"], 2)
                    self.assertFalse(e["is_dir"])
                if e["name"] == "subdir":
                    self.assertTrue(e["is_dir"])

    def test_list_missing_dir_errors(self):
        out, err = dispatch(json.dumps({"op": "fs.list", "path": "/no/such/dir_xyz"}))
        self.assertTrue(err)
        self.assertIn("directory not found", out)


class TestFsDelete(unittest.TestCase):
    """fs.delete operation."""

    def test_delete_removes_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "del.txt")
            with open(p, "w") as f:
                f.write("x")
            out, err = dispatch(json.dumps({"op": "fs.delete", "path": p}))
            self.assertFalse(err)
            self.assertFalse(os.path.exists(p))

    def test_delete_missing_file_errors(self):
        out, err = dispatch(json.dumps({"op": "fs.delete", "path": "/no/such/del_xyz.txt"}))
        self.assertTrue(err)
        self.assertIn("file not found", out)


class TestFsReplace(unittest.TestCase):
    """fs.replace operation — exact-once match semantics."""

    def test_replace_exactly_one(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("foo bar foo")
            out, err = dispatch(json.dumps({
                "op": "fs.replace", "path": p, "old": "bar", "new": "baz"
            }))
            self.assertFalse(err)
            with open(p, "r") as f:
                self.assertEqual(f.read(), "foo baz foo")

    def test_replace_zero_matches_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("nothing here")
            out, err = dispatch(json.dumps({
                "op": "fs.replace", "path": p, "old": "xyz", "new": "abc"
            }))
            self.assertTrue(err)
            self.assertIn("old text not found", out)

    def test_replace_two_matches_errors(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "r.txt")
            with open(p, "w") as f:
                f.write("dup dup")
            out, err = dispatch(json.dumps({
                "op": "fs.replace", "path": p, "old": "dup", "new": "uniq"
            }))
            self.assertTrue(err)
            self.assertIn("matches 2 times", out)

    def test_replace_missing_file_errors(self):
        out, err = dispatch(json.dumps({
            "op": "fs.replace", "path": "/no/such/repl.txt", "old": "a", "new": "b"
        }))
        self.assertTrue(err)
        self.assertIn("file not found", out)


class TestFsSearch(unittest.TestCase):
    """fs.search operation — regex grep over file content."""

    def test_search_finds_pattern(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "s.txt")
            with open(p, "w") as f:
                f.write("cat\ndog\nbat\n")
            # 'a' appears in cat and bat, not dog
            out, err = dispatch(json.dumps({"op": "fs.search", "path": p, "pattern": "a"}))
            self.assertFalse(err)
            matches = json.loads(out)
            lines = [m["line"] for m in matches]
            self.assertIn(1, lines)   # cat
            self.assertIn(3, lines)   # bat
            self.assertNotIn(2, lines)  # dog has no 'a'

    def test_search_missing_file_errors(self):
        out, err = dispatch(json.dumps({
            "op": "fs.search", "path": "/no/such/search.txt", "pattern": "x"
        }))
        self.assertTrue(err)
        self.assertIn("file not found", out)


class TestFsFind(unittest.TestCase):
    """fs.find operation — glob-like file finder."""

    def test_find_by_pattern(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "sub"))
            with open(os.path.join(d, "a.txt"), "w") as f:
                f.write("")
            with open(os.path.join(d, "b.txt"), "w") as f:
                f.write("")
            with open(os.path.join(d, "sub", "c.txt"), "w") as f:
                f.write("")
            # Use **/*.txt for recursive glob (includes subdirs)
            out, err = dispatch(json.dumps({"op": "fs.find", "path": d, "pattern": "**/*.txt"}))
            self.assertFalse(err)
            results = json.loads(out)
            names = [os.path.basename(r) for r in results]
            self.assertIn("a.txt", names)
            self.assertIn("b.txt", names)
            self.assertIn("c.txt", names)

    def test_find_missing_dir_errors(self):
        out, err = dispatch(json.dumps({
            "op": "fs.find", "path": "/no/such/find_dir", "pattern": "*"
        }))
        self.assertTrue(err)
        self.assertIn("directory not found", out)


class TestFsBinRead(unittest.TestCase):
    """fs.bin_read operation — base64-encoded binary read."""

    def test_bin_read_returns_base64_payload(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bin.dat")
            with open(p, "wb") as f:
                f.write(b"\x00\x01\x02\xff")
            out, err = dispatch(json.dumps({"op": "fs.bin_read", "path": p}))
            self.assertFalse(err)
            data = json.loads(out)
            self.assertEqual(data["path"], p)
            self.assertEqual(data["size"], 4)
            import base64
            self.assertEqual(base64.b64decode(data["b64"]), b"\x00\x01\x02\xff")

    def test_bin_read_missing_file_errors(self):
        out, err = dispatch(json.dumps({"op": "fs.bin_read", "path": "/no/such/bin_xyz.dat"}))
        self.assertTrue(err)
        self.assertIn("file not found", out)


class TestFsBinWrite(unittest.TestCase):
    """fs.bin_write operation — base64-decoded binary write."""

    def test_bin_write_roundtrips_bytes(self):
        import base64
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "out.bin")
            payload = base64.b64encode(b"\xde\xad\xbe\xef").decode("ascii")
            out, err = dispatch(json.dumps({"op": "fs.bin_write", "path": p, "b64": payload}))
            self.assertFalse(err)
            self.assertIn("wrote 4 bytes", out)
            with open(p, "rb") as f:
                self.assertEqual(f.read(), b"\xde\xad\xbe\xef")

    def test_bin_write_invalid_base64_errors(self):
        out, err = dispatch(json.dumps({"op": "fs.bin_write", "path": "/tmp/x.bin", "b64": "!!!not-base64!!!"}))
        self.assertTrue(err)
        self.assertIn("invalid base64", out)

    def test_bin_write_creates_parent_dirs(self):
        import base64
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "nested", "dir", "f.bin")
            payload = base64.b64encode(b"ab").decode("ascii")
            out, err = dispatch(json.dumps({"op": "fs.bin_write", "path": p, "b64": payload}))
            self.assertFalse(err)
            self.assertTrue(os.path.isfile(p))


class TestRegister(unittest.TestCase):
    """register operation — aliases sysinfo for agent registration."""

    def test_register_returns_sysinfo(self):
        out, err = dispatch(json.dumps({"op": "register"}))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertIn("os", data)
        self.assertIn("hostname", data)
        self.assertIn("python_version", data)

    def test_register_returns_same_as_sysinfo(self):
        reg_out, _ = dispatch(json.dumps({"op": "register"}))
        sys_out, _ = dispatch(json.dumps({"op": "sysinfo"}))
        reg_data = json.loads(reg_out)
        sys_data = json.loads(sys_out)
        # Same keys (values like mem_available may differ between calls)
        self.assertEqual(set(reg_data.keys()), set(sys_data.keys()))



class TestRegistryDispatch(unittest.TestCase):
    """T4: dispatch() uses registry.get_op_handler for op lookup."""

    def test_dispatch_uses_registry_for_lookup(self):
        """Monkey-patch a new op into registry; dispatch must call it."""
        from cliptunnel_mcp import plugins
        if not plugins._loaded:
            plugins.register_builtins(plugins.registry)
            plugins._loaded = True

        called = []
        def custom_handler(req):
            called.append(req)
            return ("custom-result", False)

        # Register a temporary op (collision-free name)
        plugins.registry._ops["test.custom"] = custom_handler
        try:
            out, err = dispatch(json.dumps({"op": "test.custom", "data": 42}))
            self.assertFalse(err)
            self.assertEqual(out, "custom-result")
            self.assertEqual(len(called), 1)
            self.assertEqual(called[0]["data"], 42)
        finally:
            plugins.registry._ops.pop("test.custom", None)

    def test_unknown_op_still_exact_error_string(self):
        out, err = dispatch(json.dumps({"op": "truly.nonexistent"}))
        self.assertTrue(err)
        self.assertEqual(out, "unknown op: truly.nonexistent")

    def test_all_existing_ops_still_work(self):
        """All built-in ops must still dispatch correctly."""
        out, err = dispatch(json.dumps({"op": "shell", "cmd": "echo ok"}))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertEqual(data["stdout"].strip(), "ok")
if __name__ == "__main__":
    unittest.main()
