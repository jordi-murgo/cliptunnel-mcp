"""Unit tests for cliptunnel_mcp.operations — stdlib unittest, zero deps.

Ported from vulcano-helper tests/test_operations.py (minus the Vulcano-specific
agent/copilot op) with the same JSON request/response shapes and error strings.

Tests run on macOS (the test machine).  Uses tempfile.TemporaryDirectory
for all filesystem tests so nothing touches the real filesystem, and never
touches the clipboard — dispatch is a pure function.
"""
import base64
import hashlib
import json
import os
import tempfile
import time
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
        if not plugins._builtins_loaded:
            plugins.register_builtins(plugins.registry)
            plugins._builtins_loaded = True

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


class TestFileTransferStart(unittest.TestCase):
    """file.transfer.start op tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_start_upload_success(self) -> None:
        filename = os.path.join(self._tmp.name, "x.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "filename": filename,
            "size": 65536,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertIsInstance(data["transfer_id"], str)
        self.assertEqual(data["total_blocks"], 1)

    def test_start_download_success(self) -> None:
        filename = os.path.join(self._tmp.name, "dl.bin")
        with open(filename, "wb") as f:
            f.write(b"\x00" * 131072)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": filename,
            "size": 131072,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertIsInstance(data["transfer_id"], str)
        self.assertEqual(data["total_blocks"], 2)

    def test_start_download_size_hint_ignored(self) -> None:
        filename = os.path.join(self._tmp.name, "dl_hint.bin")
        with open(filename, "wb") as f:
            f.write(b"\x00" * 102400)  # 100KB
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": filename,
            "size": 999,  # wrong hint
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        data = json.loads(out)
        # total_blocks from real file size (102400 / 65536 = 2)
        self.assertEqual(data["total_blocks"], 2)

    def test_start_missing_filename(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "size": 65536,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertTrue(err)
        self.assertIn("missing 'filename' field", out)

    def test_start_missing_direction(self) -> None:
        filename = os.path.join(self._tmp.name, "no_dir.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "filename": filename,
            "size": 65536,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertTrue(err)
        self.assertIn("direction", out)

    def test_start_invalid_direction(self) -> None:
        filename = os.path.join(self._tmp.name, "bad_dir.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "sideways",
            "filename": filename,
            "size": 65536,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertTrue(err)
        self.assertTrue("upload" in out or "download" in out)

    def test_start_download_nonexistent_file(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": "/no/such/file",
            "size": 65536,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertTrue(err)
        self.assertIn("not found", out)

    def test_start_block_size_exceeds_safe_max(self) -> None:
        filename = os.path.join(self._tmp.name, "big_block.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "filename": filename,
            "size": 65536,
            "block_size": 999999999,
            "checksum": "abc",
        }))
        self.assertTrue(err)
        self.assertTrue("exceeds" in out or "safe maximum" in out)


class TestFileTransferBlock(unittest.TestCase):
    """file.transfer.block op tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _start_upload(self, size: int = 131072, block_size: int = 65536) -> str:
        filename = os.path.join(self._tmp.name, "up.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "filename": filename,
            "size": size,
            "block_size": block_size,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def _start_download(self, content: bytes, block_size: int = 65536) -> str:
        filename = os.path.join(self._tmp.name, "dl.bin")
        with open(filename, "wb") as f:
            f.write(content)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": filename,
            "size": len(content),
            "block_size": block_size,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def test_block_upload_success(self) -> None:
        import base64
        tid = self._start_upload(size=65536, block_size=65536)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid,
            "block_num": 0,
            "data": base64.b64encode(b"hello").decode("ascii"),
        }))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertEqual(data["block_num"], 0)
        self.assertEqual(data["total_blocks"], 1)
        self.assertEqual(data["status"], "ok")

    def test_block_download_success(self) -> None:
        tid = self._start_download(b"A" * 65536 + b"B" * 100)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid,
            "block_num": 0,
        }))
        self.assertFalse(err)
        data = json.loads(out)
        self.assertEqual(data["block_num"], 0)
        self.assertIn("data", data)
        self.assertEqual(data["total_blocks"], 2)
        self.assertEqual(data["status"], "ok")

    def test_block_invalid_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": "nonexistent",
            "block_num": 0,
            "data": "AAAA",
        }))
        self.assertTrue(err)
        self.assertIn("invalid or expired", out)

    def test_block_out_of_order(self) -> None:
        import base64
        tid = self._start_upload(size=5 * 65536, block_size=65536)  # total_blocks=5
        dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 0,
            "data": base64.b64encode(b"a").decode("ascii"),
        }))
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 3,  # skips 1,2 but within total_blocks
            "data": base64.b64encode(b"b").decode("ascii"),
        }))
        self.assertTrue(err)
        self.assertIn("out of order", out)

    def test_block_duplicate(self) -> None:
        import base64
        tid = self._start_upload()
        b64 = base64.b64encode(b"a").decode("ascii")
        dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 0, "data": b64,
        }))
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 0, "data": b64,
        }))
        self.assertTrue(err)
        self.assertIn("duplicate", out)

    def test_block_exceeds_total(self) -> None:
        import base64
        tid = self._start_upload(size=65536, block_size=65536)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 1,
            "data": base64.b64encode(b"x").decode("ascii"),
        }))
        self.assertTrue(err)
        self.assertIn("exceeds total_blocks", out)

    def test_block_on_cancelled_session(self) -> None:
        import base64
        tid = self._start_upload()
        dispatch(json.dumps({"op": "file.transfer.cancel", "transfer_id": tid}))
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 0,
            "data": base64.b64encode(b"x").decode("ascii"),
        }))
        self.assertTrue(err)
        self.assertIn("invalid or expired", out)

    def test_block_missing_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "block_num": 0,
            "data": "AAAA",
        }))
        self.assertTrue(err)
        self.assertIn("missing 'transfer_id' field", out)

    def test_block_missing_block_num(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": "some_id",
            "data": "AAAA",
        }))
        self.assertTrue(err)
        self.assertIn("missing 'block_num' field", out)

    def test_block_upload_missing_data(self) -> None:
        tid = self._start_upload(size=65536, block_size=65536)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid,
            "block_num": 0,
        }))
        self.assertTrue(err)
        self.assertIn("missing 'data' field", out)


class TestFileTransferEnd(unittest.TestCase):
    """file.transfer.end op tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _start_upload(self, size: int, block_size: int, checksum: str) -> str:
        filename = os.path.join(self._tmp.name, "end_up.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "filename": filename,
            "size": size,
            "block_size": block_size,
            "checksum": checksum,
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def _start_download(self, content: bytes, checksum: str = "abc") -> str:
        filename = os.path.join(self._tmp.name, "end_dl.bin")
        with open(filename, "wb") as f:
            f.write(content)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": filename,
            "size": len(content),
            "block_size": 65536,
            "checksum": checksum,
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def _send_block(self, tid: str, block_num: int, data: bytes) -> None:
        import base64
        out, err = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid,
            "block_num": block_num,
            "data": base64.b64encode(data).decode("ascii"),
        }))
        self.assertFalse(err)

    def test_end_upload_success_matching_checksum(self) -> None:
        import hashlib
        data = b"hello world"
        checksum = hashlib.sha256(data).hexdigest()
        tid = self._start_upload(len(data), 65536, checksum)
        self._send_block(tid, 0, data)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.end", "transfer_id": tid,
        }))
        self.assertFalse(err)
        resp = json.loads(out)
        self.assertTrue(resp["verified"])
        self.assertIn("path", resp)
        self.assertEqual(resp["size"], len(data))
        # Final file exists with correct content
        filename = os.path.join(self._tmp.name, "end_up.bin")
        self.assertTrue(os.path.isfile(filename))
        with open(filename, "rb") as f:
            self.assertEqual(f.read(), data)

    def test_end_upload_checksum_mismatch(self) -> None:
        data = b"actual data"
        wrong_checksum = hashlib.sha256(b"different").hexdigest()
        tid = self._start_upload(len(data), 65536, wrong_checksum)
        self._send_block(tid, 0, data)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.end", "transfer_id": tid,
        }))
        self.assertFalse(err)
        resp = json.loads(out)
        self.assertFalse(resp["verified"])
        # Final path does NOT exist
        filename = os.path.join(self._tmp.name, "end_up.bin")
        self.assertFalse(os.path.exists(filename))

    def test_end_download_success(self) -> None:
        content = b"download me"
        tid = self._start_download(content)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.end", "transfer_id": tid,
        }))
        self.assertFalse(err)
        resp = json.loads(out)
        self.assertTrue(resp["verified"])
        self.assertIn("path", resp)
        self.assertEqual(resp["size"], len(content))

    def test_end_invalid_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.end", "transfer_id": "nonexistent",
        }))
        self.assertTrue(err)
        self.assertIn("invalid or expired", out)

    def test_end_before_all_blocks_received(self) -> None:
        tid = self._start_upload(5 * 65536, 65536, "abc")
        self._send_block(tid, 0, b"\x00" * 65536)
        self._send_block(tid, 1, b"\x00" * 65536)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.end", "transfer_id": tid,
        }))
        self.assertTrue(err)
        self.assertIn("not all blocks", out)

    def test_end_missing_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({"op": "file.transfer.end"}))
        self.assertTrue(err)
        self.assertIn("missing 'transfer_id' field", out)


class TestFileTransferCancel(unittest.TestCase):
    """file.transfer.cancel op tests."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _start_upload(self, size: int = 1048576, block_size: int = 65536) -> str:
        filename = os.path.join(self._tmp.name, "cancel_up.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload",
            "filename": filename,
            "size": size,
            "block_size": block_size,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def _start_download(self) -> str:
        filename = os.path.join(self._tmp.name, "cancel_dl.bin")
        with open(filename, "wb") as f:
            f.write(b"\x00" * 1024)
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "download",
            "filename": filename,
            "size": 1024,
            "block_size": 65536,
            "checksum": "abc",
        }))
        self.assertFalse(err)
        return json.loads(out)["transfer_id"]

    def test_cancel_during_upload(self) -> None:
        import base64
        tid = self._start_upload(size=10 * 65536, block_size=65536)
        # Send 3 blocks
        for i in range(3):
            dispatch(json.dumps({
                "op": "file.transfer.block",
                "transfer_id": tid, "block_num": i,
                "data": base64.b64encode(b"\x00" * 65536).decode("ascii"),
            }))
        out, err = dispatch(json.dumps({
            "op": "file.transfer.cancel", "transfer_id": tid,
        }))
        self.assertFalse(err)
        resp = json.loads(out)
        self.assertTrue(resp["cancelled"])
        # Subsequent block op returns error
        out2, err2 = dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 3,
            "data": base64.b64encode(b"x").decode("ascii"),
        }))
        self.assertTrue(err2)
        self.assertIn("invalid or expired", out2)

    def test_cancel_during_download(self) -> None:
        tid = self._start_download()
        out, err = dispatch(json.dumps({
            "op": "file.transfer.cancel", "transfer_id": tid,
        }))
        self.assertFalse(err)
        resp = json.loads(out)
        self.assertTrue(resp["cancelled"])

    def test_cancel_invalid_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({
            "op": "file.transfer.cancel", "transfer_id": "nonexistent",
        }))
        self.assertTrue(err)
        self.assertIn("invalid or expired", out)

    def test_cancel_after_end(self) -> None:
        import hashlib
        data = b"complete file"
        checksum = hashlib.sha256(data).hexdigest()
        filename = os.path.join(self._tmp.name, "end_then_cancel.bin")
        out, err = dispatch(json.dumps({
            "op": "file.transfer.start",
            "direction": "upload", "filename": filename,
            "size": len(data), "block_size": 65536, "checksum": checksum,
        }))
        tid = json.loads(out)["transfer_id"]
        import base64
        dispatch(json.dumps({
            "op": "file.transfer.block",
            "transfer_id": tid, "block_num": 0,
            "data": base64.b64encode(data).decode("ascii"),
        }))
        dispatch(json.dumps({"op": "file.transfer.end", "transfer_id": tid}))
        out2, err2 = dispatch(json.dumps({
            "op": "file.transfer.cancel", "transfer_id": tid,
        }))
        self.assertTrue(err2)
        self.assertIn("invalid or expired", out2)

    def test_cancel_missing_transfer_id(self) -> None:
        out, err = dispatch(json.dumps({"op": "file.transfer.cancel"}))
        self.assertTrue(err)
        self.assertIn("missing 'transfer_id' field", out)


class TestTransferSessionTimeoutOps(unittest.TestCase):
    """file.transfer session timeout via op dispatch."""

    def test_session_timeout_auto_cleanup(self) -> None:
        from cliptunnel_mcp.transfer_session import TransferSessionManager
        import base64
        with tempfile.TemporaryDirectory() as d:
            mgr = TransferSessionManager(timeout_secs=0.5)
            filename = os.path.join(d, "timeout.bin")
            tid, _ = mgr.create_session(
                "upload", filename, size=1048576, block_size=65536, checksum="abc",
            )
            mgr.append_block(tid, 0, b"data")
            time.sleep(0.7)
            mgr._sweep_once()
            self.assertIsNone(mgr.get_session(tid))
            # dispatch block op with that transfer_id → "invalid or expired"
            out, err = dispatch(json.dumps({
                "op": "file.transfer.block",
                "transfer_id": tid, "block_num": 1,
                "data": base64.b64encode(b"x").decode("ascii"),
            }))
            self.assertTrue(err)
            self.assertIn("invalid or expired", out)
            mgr.close()

    def test_session_timeout_after_no_activity(self) -> None:
        from cliptunnel_mcp.transfer_session import TransferSessionManager
        with tempfile.TemporaryDirectory() as d:
            mgr = TransferSessionManager(timeout_secs=5.0)
            filename = os.path.join(d, "alive.bin")
            tid, _ = mgr.create_session(
                "upload", filename, size=1048576, block_size=65536, checksum="abc",
            )
            mgr.append_block(tid, 0, b"data")
            time.sleep(0.3)
            self.assertIsNotNone(mgr.get_session(tid))
            mgr.close()


class TestTransferBackwardCompat(unittest.TestCase):
    """Backward compatibility: existing ops unchanged + new ops registered."""

    def test_fs_bin_write_still_works(self) -> None:
        import base64
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "bw.bin")
            payload = base64.b64encode(b"\xde\xad\xbe\xef").decode("ascii")
            out, err = dispatch(json.dumps({"op": "fs.bin_write", "path": p, "b64": payload}))
            self.assertFalse(err)
            self.assertIn("wrote 4 bytes", out)

    def test_fs_bin_read_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "br.bin")
            with open(p, "wb") as f:
                f.write(b"\x00\x01\x02\xff")
            out, err = dispatch(json.dumps({"op": "fs.bin_read", "path": p}))
            self.assertFalse(err)
            data = json.loads(out)
            self.assertEqual(data["size"], 4)

    def test_all_existing_ops_still_registered(self) -> None:
        from cliptunnel_mcp import plugins
        if not plugins._builtins_loaded:
            plugins.register_builtins(plugins.registry)
            plugins._builtins_loaded = True
        ops = set(plugins.registry.op_names())
        # Existing ops
        for name in ("shell", "fs.read", "fs.write", "fs.list", "fs.delete",
                      "fs.bin_read", "fs.bin_write", "sysinfo", "agent"):
            self.assertIn(name, ops)
        # New ops
        for name in ("file.transfer.start", "file.transfer.block",
                      "file.transfer.end", "file.transfer.cancel"):
            self.assertIn(name, ops)


if __name__ == "__main__":
    unittest.main()
