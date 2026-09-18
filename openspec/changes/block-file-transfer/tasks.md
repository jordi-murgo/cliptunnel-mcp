# Tasks: Block-Based File Transfer

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 800–1100 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 (config + TransferSessionManager + op handlers) → PR 2 (plugins + MCP tools + upload/download refactor) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Config + `TransferSessionManager` + 4 op handlers + all unit tests | PR 1 | `python -m unittest tests.test_transfer_session tests.test_operations tests.test_config -v` | N/A — pure unit tests via `dispatch()` and direct manager instantiation; no transport needed | Revert PR 1 — removes `transfer_session.py`, op handlers, config entries; existing ops unchanged |
| 2 | `plugins.py` registration + 4 MCP `@mcp.tool()` decorators + `ToolSpec` entries + refactored `upload()`/`download()` block loops + integration tests | PR 2 | `python -m unittest tests.test_server tests.test_plugins -v` | `ServerTestCase` with `ClipboardSlot` + real `Agent` + `Controller` (existing test harness) | Revert PR 2 — removes MCP tools and upload/download refactor; PR 1's op handlers remain standalone |

## Phase 1: Foundation — Config and Transfer Session Manager

- [x] 1.1 **RED**: Write failing tests in `tests/test_config.py` for `CLIPTUNNEL_BLOCK_SIZE` and `CLIPTUNNEL_TRANSFER_TIMEOUT_SECS` env/TOML/default precedence. Test cases: (a) default 65536 when neither env nor TOML set, (b) env var `CLIPTUNNEL_BLOCK_SIZE=131072` overrides TOML, (c) TOML `[transfer] block_size=32768` used when env unset, (d) `CLIPTUNNEL_TRANSFER_TIMEOUT_SECS=30` via env, (e) env precedence over TOML for timeout. Run: `python -m unittest tests.test_config -v` — expect failures (no `transfer` section in `ENV_TO_FILE`).
  - Files: `tests/test_config.py`
  - Depends on: nothing
  - Verify: `python -m unittest tests.test_config -v` (5 new tests fail)

- [x] 1.2 **GREEN**: Add `"CLIPTUNNEL_BLOCK_SIZE": (("transfer",), "block_size")` and `"CLIPTUNNEL_TRANSFER_TIMEOUT_SECS": (("transfer",), "timeout_secs")` to `ENV_TO_FILE` in `src/cliptunnel_mcp/config.py`. Run: `python -m unittest tests.test_config -v` — all tests pass.
  - Files: `src/cliptunnel_mcp/config.py`
  - Depends on: 1.1
  - Verify: `python -m unittest tests.test_config -v` (all pass)
  - Commit: `feat(config): add CLIPTUNNEL_BLOCK_SIZE and CLIPTUNNEL_TRANSFER_TIMEOUT_SECS to ENV_TO_FILE`

- [ ] 1.3 **RED**: Create `tests/test_transfer_session.py` with failing unit tests for `TransferSessionManager` core API. Test class `TestTransferSessionManager` using `tempfile.TemporaryDirectory`:
  - (a) `test_create_session_upload` — `create_session("upload", filename, size=1048576, block_size=65536, checksum)` returns `(transfer_id, 16)`; `get_session(transfer_id)` returns a `TransferSession` with correct fields
  - (b) `test_create_session_download` — `create_session("download", ...)` with existing file returns `(transfer_id, total_blocks)`; for non-existent file raises/returns error
  - (c) `test_total_blocks_ceiling_division` — size=65537, block_size=65536 → total_blocks=2
  - (d) `test_append_block_upload` — create session, `append_block(tid, 0, b"data")`, temp file grows by 4 bytes, `session.received_blocks == 1`
  - (e) `test_append_block_out_of_order` — after block 0, sending block 3 raises `ValueError` with "out of order"
  - (f) `test_append_block_duplicate` — sending block 0 twice raises `ValueError` with "duplicate"
  - (g) `test_append_block_exceeds_total` — sending block_num >= total_blocks raises `ValueError`
  - (h) `test_read_block_download` — create download session for a real file, `read_block(tid, 0)` returns first `block_size` bytes; `read_block(tid, 1)` returns next slice; last block returns remaining bytes
  - (i) `test_finalize_upload_matching_checksum` — create upload session, append all blocks with known content, `finalize_upload(tid)` returns `(path, size, True)`; temp file renamed to final path; final path contains correct content; temp path no longer exists
  - (j) `test_finalize_upload_checksum_mismatch` — append blocks with wrong data, `finalize_upload(tid)` returns `(temp_path, size, False)`; temp file retained; final path does not exist
  - (k) `test_finalize_upload_incomplete` — append fewer blocks than total_blocks, `finalize_upload(tid)` raises `ValueError` "not all blocks received"
  - (l) `test_finalize_download` — `finalize_download(tid)` returns `(path, size)`; session cleaned up
  - (m) `test_cancel_upload` — create session, append 2 blocks, `cancel(tid)` returns `True`; temp file deleted; `get_session(tid)` returns `None`
  - (n) `test_cancel_download` — create download session, `cancel(tid)` returns `True`; `get_session(tid)` returns `None`
  - (o) `test_cancel_invalid_id` — `cancel("nonexistent")` returns `False`
  - (p) `test_get_session_invalid_id` — `get_session("nonexistent")` returns `None`
  - (q) `test_concurrent_sessions` — create two sessions, append blocks to both interleaved, each accumulates independently
  - Run: `python -m unittest tests.test_transfer_session -v` — all fail (module doesn't exist).
  - Files: `tests/test_transfer_session.py`
  - Depends on: nothing (tests define the contract)
  - Verify: `python -m unittest tests.test_transfer_session -v` (17 tests fail)

- [ ] 1.4 **GREEN**: Create `src/cliptunnel_mcp/transfer_session.py` implementing `TransferSession` dataclass and `TransferSessionManager` class per the design's interface contract. Include:
  - `TransferSession` dataclass: `transfer_id`, `direction`, `filename`, `block_size`, `total_blocks`, `expected_checksum`, `temp_path`, `received_blocks=0`, `last_activity`
  - `TransferSessionManager.__init__`: `_sessions` dict, `threading.Lock`, timeout from config or default 60s, daemon sweep thread
  - `create_session(direction, filename, size, block_size, checksum)`: validate direction; for download verify file exists and compute real size via `os.path.getsize()` (ignore controller's `size` hint); for upload create temp file via `tempfile.NamedTemporaryFile(delete=False, dir=parent_of_final_path)`; compute `total_blocks = ceil(size / block_size)`; store session; return `(transfer_id, total_blocks)`
  - `get_session(transfer_id)`: return session or `None`
  - `append_block(transfer_id, block_num, data)`: validate session exists; validate `block_num == received_blocks` (raise ValueError for out-of-order/duplicate/exceeds); append bytes to temp file; increment `received_blocks`; update `last_activity`
  - `read_block(transfer_id, block_num)`: validate session; seek `block_num * block_size` in remote file; read `block_size` bytes; update `last_activity`; return bytes
  - `finalize_upload(transfer_id)`: validate session; check `received_blocks == total_blocks`; compute SHA-256 of temp file; if matches → `os.replace(temp, final)`, cleanup session, return `(final_path, size, True)`; if mismatch → retain temp, return `(temp_path, size, False)`
  - `finalize_download(transfer_id)`: cleanup session, return `(path, size)` from `os.path.getsize()`
  - `cancel(transfer_id)`: delete temp file if upload, remove session, return `True`; return `False` if not found
  - `_sweep()`: loop while `_running`, sleep `SWEEP_INTERVAL_SECS`, check all sessions for `time.time() - last_activity > timeout_secs`, delete expired sessions' temp files and remove them
  - `close()`: set `_running = False`, join sweep thread, cleanup all sessions
  - Platform safe maximum: `_platform_safe_max_block_size()` — 4MB on Windows, 16MB otherwise; `create_session` rejects `block_size > safe_max` with `ValueError`
  - Run: `python -m unittest tests.test_transfer_session -v` — all 17 tests pass.
  - Files: `src/cliptunnel_mcp/transfer_session.py`
  - Depends on: 1.3
  - Verify: `python -m unittest tests.test_transfer_session -v` (all pass)
  - Commit: `feat(transfer): add TransferSessionManager with session lifecycle, block append, checksum verify, and timeout sweep`

## Phase 2: Core Implementation — Op Handlers

- [ ] 2.1 **RED**: Write failing tests in `tests/test_operations.py` for `file.transfer.start` op via `dispatch()`. New test class `TestFileTransferStart`:
  - (a) `test_start_upload_success` — `dispatch(json.dumps({"op":"file.transfer.start","direction":"upload","filename":"/tmp/x.bin","size":65536,"block_size":65536,"checksum":"abc"}))` returns `({"transfer_id":"...","total_blocks":1}, False)`; parse JSON, assert `transfer_id` is a string, `total_blocks == 1`
  - (b) `test_start_download_success` — create a real temp file, dispatch with `direction:"download"`, assert `total_blocks` matches real file size / block_size
  - (c) `test_start_download_size_hint_ignored` — create a 100KB file, send `size: 999` (wrong hint), assert `total_blocks` computed from real `os.path.getsize()`, not 999
  - (d) `test_start_missing_filename` — omit `filename`, assert error string `"missing 'filename' field"`, `is_error=True`
  - (e) `test_start_missing_direction` — omit `direction`, assert error string mentions direction
  - (f) `test_start_invalid_direction` — `direction:"sideways"`, assert error mentions `"upload"` or `"download"`, `is_error=True`
  - (g) `test_start_download_nonexistent_file` — `direction:"download"`, `filename:"/no/such/file"`, assert error mentions "not found", `is_error=True`
  - (h) `test_start_block_size_exceeds_safe_max` — `block_size: 999999999`, assert error mentions "exceeds" or "safe maximum", `is_error=True`
  - Run: `python -m unittest tests.test_operations.TestFileTransferStart -v` — all fail.
  - Files: `tests/test_operations.py`
  - Depends on: 1.4 (TransferSessionManager exists for test imports)
  - Verify: `python -m unittest tests.test_operations.TestFileTransferStart -v` (8 tests fail)

- [ ] 2.2 **GREEN**: Implement `op_file_transfer_start(req)` in `src/cliptunnel_mcp/operations.py`:
  - Parse `direction`, `filename`, `size`, `block_size`, `checksum` from `req`; validate each required field (return `(f"missing '{field}' field", True)` if absent)
  - Validate `direction` is `"upload"` or `"download"`
  - Validate `block_size` against `_platform_safe_max_block_size()` (import from `transfer_session.py`)
  - Lazy-init module-level singleton `_transfer_sessions: TransferSessionManager | None = None` (same pattern as `op_agent._sessions`)
  - Call `_transfer_sessions.create_session(...)`; return `(json.dumps({"transfer_id": tid, "total_blocks": tb}), False)`
  - On `ValueError` from `create_session` (file not found for download, block size too large), return `(str(exc), True)`
  - Run: `python -m unittest tests.test_operations.TestFileTransferStart -v` — all pass.
  - Files: `src/cliptunnel_mcp/operations.py`
  - Depends on: 2.1
  - Verify: `python -m unittest tests.test_operations.TestFileTransferStart -v` (all pass)

- [ ] 2.3 **RED**: Write failing tests in `tests/test_operations.py` for `file.transfer.block` op. New test class `TestFileTransferBlock`:
  - (a) `test_block_upload_success` — start a session, dispatch `file.transfer.block` with `transfer_id`, `block_num: 0`, `data: base64.b64encode(b"hello")`, assert response JSON `{"block_num":0, "total_blocks":N, "status":"ok"}`, `is_error=False`
  - (b) `test_block_download_success` — start a download session for a real file, dispatch `file.transfer.block` with `transfer_id`, `block_num: 0` (no `data` field), assert response JSON has `block_num`, `data` (base64 string), `total_blocks`, `status:"ok"`, `is_error=False`
  - (c) `test_block_invalid_transfer_id` — `transfer_id:"nonexistent"`, assert error "invalid or expired", `is_error=True`
  - (d) `test_block_out_of_order` — start session, send block 0, then block 3 (skip 1,2), assert error "out of order", `is_error=True`
  - (e) `test_block_duplicate` — start session, send block 0 twice, assert error "duplicate", `is_error=True`
  - (f) `test_block_exceeds_total` — start session with `total_blocks=1`, send `block_num: 1`, assert error "exceeds total_blocks", `is_error=True`
  - (g) `test_block_on_cancelled_session` — start session, cancel it, then send block, assert error "invalid or expired", `is_error=True`
  - (h) `test_block_missing_transfer_id` — omit `transfer_id`, assert error `"missing 'transfer_id' field"`, `is_error=True`
  - (i) `test_block_missing_block_num` — omit `block_num`, assert error `"missing 'block_num' field"`, `is_error=True`
  - (j) `test_block_upload_missing_data` — upload direction, omit `data`, assert error `"missing 'data' field"`, `is_error=True`
  - Run: `python -m unittest tests.test_operations.TestFileTransferBlock -v` — all fail.
  - Files: `tests/test_operations.py`
  - Depends on: 2.2 (op_file_transfer_start works so tests can create sessions)
  - Verify: `python -m unittest tests.test_operations.TestFileTransferBlock -v` (10 tests fail)

- [ ] 2.4 **GREEN**: Implement `op_file_transfer_block(req)` in `src/cliptunnel_mcp/operations.py`:
  - Parse `transfer_id`, `block_num`; validate presence
  - Get session via `_transfer_sessions.get_session(transfer_id)`; if None → `("transfer_id invalid or expired", True)`
  - If `direction == "upload"`: parse `data` (base64); validate presence; `base64.b64decode(data, validate=True)`; call `_transfer_sessions.append_block(tid, block_num, decoded)`; on `ValueError` return `(str(exc), True)` with appropriate messages ("out of order", "duplicate block_num", "exceeds total_blocks")
  - If `direction == "download"`: call `_transfer_sessions.read_block(tid, block_num)`; encode result to base64; return `(json.dumps({"block_num": block_num, "data": b64, "total_blocks": session.total_blocks, "status": "ok"}), False)`
  - For upload success: return `(json.dumps({"block_num": block_num, "total_blocks": session.total_blocks, "status": "ok"}), False)`
  - Run: `python -m unittest tests.test_operations.TestFileTransferBlock -v` — all pass.
  - Files: `src/cliptunnel_mcp/operations.py`
  - Depends on: 2.3
  - Verify: `python -m unittest tests.test_operations.TestFileTransferBlock -v` (all pass)

- [ ] 2.5 **RED**: Write failing tests in `tests/test_operations.py` for `file.transfer.end` op. New test class `TestFileTransferEnd`:
  - (a) `test_end_upload_success_matching_checksum` — start upload session, send all blocks with correct data, dispatch `file.transfer.end`, assert response JSON `{"path":"...","size":N,"verified":true}`, `is_error=False`; verify final file exists with correct content; temp file gone
  - (b) `test_end_upload_checksum_mismatch` — send blocks with wrong data, dispatch end, assert response JSON `{"verified":false}`, `is_error=False`; final path does NOT exist; temp file retained
  - (c) `test_end_download_success` — start download session for real file, dispatch end, assert response JSON `{"path":"...","size":N,"verified":true}`, `is_error=False`
  - (d) `test_end_invalid_transfer_id` — `transfer_id:"nonexistent"`, assert error "invalid or expired", `is_error=True`
  - (e) `test_end_before_all_blocks_received` — start session with `total_blocks=5`, send only 2 blocks, dispatch end, assert error "not all blocks have been received", `is_error=True`; verify file NOT renamed
  - (f) `test_end_missing_transfer_id` — omit `transfer_id`, assert error `"missing 'transfer_id' field"`, `is_error=True`
  - Run: `python -m unittest tests.test_operations.TestFileTransferEnd -v` — all fail.
  - Files: `tests/test_operations.py`
  - Depends on: 2.4 (block op works so tests can populate blocks)
  - Verify: `python -m unittest tests.test_operations.TestFileTransferEnd -v` (6 tests fail)

- [ ] 2.6 **GREEN**: Implement `op_file_transfer_end(req)` in `src/cliptunnel_mcp/operations.py`:
  - Parse `transfer_id`; validate presence
  - Get session; if None → `("transfer_id invalid or expired", True)`
  - If `direction == "upload"`: call `_transfer_sessions.finalize_upload(tid)`; on `ValueError` ("not all blocks received") return `(str(exc), True)`; on success return `(json.dumps({"path": path, "size": size, "verified": verified}), False)`; on `verified=False` return `(json.dumps({"verified": False}), False)`
  - If `direction == "download"`: call `_transfer_sessions.finalize_download(tid)`; return `(json.dumps({"path": path, "size": size, "verified": True}), False)`
  - Run: `python -m unittest tests.test_operations.TestFileTransferEnd -v` — all pass.
  - Files: `src/cliptunnel_mcp/operations.py`
  - Depends on: 2.5
  - Verify: `python -m unittest tests.test_operations.TestFileTransferEnd -v` (all pass)

- [ ] 2.7 **RED**: Write failing tests in `tests/test_operations.py` for `file.transfer.cancel` op. New test class `TestFileTransferCancel`:
  - (a) `test_cancel_during_upload` — start upload session, send 3 of 10 blocks, dispatch `file.transfer.cancel`, assert response JSON `{"cancelled":true}`, `is_error=False`; verify temp file deleted; subsequent block op returns "invalid or expired"
  - (b) `test_cancel_during_download` — start download session, dispatch cancel, assert `{"cancelled":true}`, `is_error=False`; session gone
  - (c) `test_cancel_invalid_transfer_id` — `transfer_id:"nonexistent"`, assert error "invalid or expired", `is_error=True`
  - (d) `test_cancel_after_end` — start session, complete transfer with end, then dispatch cancel, assert error "invalid or expired", `is_error=True`
  - (e) `test_cancel_missing_transfer_id` — omit `transfer_id`, assert error `"missing 'transfer_id' field"`, `is_error=True`
  - Run: `python -m unittest tests.test_operations.TestFileTransferCancel -v` — all fail.
  - Files: `tests/test_operations.py`
  - Depends on: 2.6 (end op works so test (d) can complete a transfer first)
  - Verify: `python -m unittest tests.test_operations.TestFileTransferCancel -v` (5 tests fail)

- [ ] 2.8 **GREEN**: Implement `op_file_transfer_cancel(req)` in `src/cliptunnel_mcp/operations.py`:
  - Parse `transfer_id`; validate presence
  - Call `_transfer_sessions.cancel(tid)`; if `True` → `(json.dumps({"cancelled": True}), False)`; if `False` → `("transfer_id invalid or expired", True)`
  - Run: `python -m unittest tests.test_operations.TestFileTransferCancel -v` — all pass.
  - Files: `src/cliptunnel_mcp/operations.py`
  - Depends on: 2.7
  - Verify: `python -m unittest tests.test_operations.TestFileTransferCancel -v` (all pass)

- [ ] 2.9 **RED**: Write failing tests in `tests/test_operations.py` for `file.transfer` session timeout. New test class `TestTransferSessionTimeout`:
  - (a) `test_session_timeout_auto_cleanup` — create `TransferSessionManager(timeout_secs=0.5)` via direct instantiation in test, create upload session, append 1 block, sleep 1.5s, assert `get_session(tid)` returns `None`; assert temp file deleted; dispatch block op with that `transfer_id` → "invalid or expired"
  - (b) `test_session_timeout_after_no_activity` — create session, append blocks, then wait; session still alive while within timeout; after timeout, session expired
  - Run: `python -m unittest tests.test_operations.TestTransferSessionTimeout -v` — fail.
  - Files: `tests/test_operations.py`
  - Depends on: 2.8
  - Verify: `python -m unittest tests.test_operations.TestTransferSessionTimeout -v` (2 tests fail)

- [ ] 2.10 **GREEN**: Verify timeout sweep works end-to-end. The `TransferSessionManager` from 1.4 already implements the sweep thread; this task ensures the op handlers' lazy singleton uses the config timeout and that the sweep correctly cleans up. If the singleton needs a configurable timeout, update `op_file_transfer_start` to read `CLIPTUNNEL_TRANSFER_TIMEOUT_SECS` via `config.get_env` when initializing the singleton. Run: `python -m unittest tests.test_operations.TestTransferSessionTimeout -v` — all pass.
  - Files: `src/cliptunnel_mcp/operations.py` (if timeout config wiring needed)
  - Depends on: 2.9
  - Verify: `python -m unittest tests.test_operations.TestTransferSessionTimeout -v` (all pass)

- [ ] 2.11 **RED**: Write backward-compatibility tests in `tests/test_operations.py` — new test class `TestTransferBackwardCompat`:
  - (a) `test_fs_bin_write_still_works` — dispatch `fs.bin_write`, assert unchanged behavior
  - (b) `test_fs_bin_read_still_works` — dispatch `fs.bin_read`, assert unchanged behavior
  - (c) `test_all_existing_ops_still_registered` — assert `registry.op_names()` includes all original ops AND the four new `file.transfer.*` ops
  - Run: `python -m unittest tests.test_operations.TestTransferBackwardCompat -v` — (a) and (b) pass (no change needed); (c) fails because new ops not yet registered.
  - Files: `tests/test_operations.py`
  - Depends on: 2.10
  - Verify: `python -m unittest tests.test_operations.TestTransferBackwardCompat -v` (test (c) fails)

- [ ] 2.12 **GREEN**: Register the four new ops in `register_builtins()` in `src/cliptunnel_mcp/plugins.py`:
  - Add: `reg.register_op("file.transfer.start", operations.op_file_transfer_start)`, `reg.register_op("file.transfer.block", operations.op_file_transfer_block)`, `reg.register_op("file.transfer.end", operations.op_file_transfer_end)`, `reg.register_op("file.transfer.cancel", operations.op_file_transfer_cancel)`
  - Run: `python -m unittest tests.test_operations.TestTransferBackwardCompat -v` — all pass. Then run full operations suite: `python -m unittest tests.test_operations -v` — all pass.
  - Files: `src/cliptunnel_mcp/plugins.py`
  - Depends on: 2.11
  - Verify: `python -m unittest tests.test_operations -v` (all pass, including all new + existing tests)
  - Commit: `feat(ops): add file.transfer.start/block/end/cancel op handlers with session management`

## Phase 3: Integration — MCP Tools and Upload/Download Refactor

- [ ] 3.1 **RED**: Write failing tests in `tests/test_server.py` for the four new MCP tools. New test class `TestTransferTools(ServerTestCase)`:
  - (a) `test_remote_file_transfer_start` — `call_json("remote_file_transfer_start", filename=remote_path, direction="upload", size=1024, block_size=65536, checksum="abc")`, assert response has `transfer_id` and `total_blocks`
  - (b) `test_remote_file_transfer_block` — start a session, then `call_json("remote_file_transfer_block", transfer_id=tid, block_num=0, data=base64.b64encode(b"data").decode())`, assert `block_num`, `status:"ok"`
  - (c) `test_remote_file_transfer_end` — start + send all blocks + `call_json("remote_file_transfer_end", transfer_id=tid)`, assert `verified:true`, `path`, `size`
  - (d) `test_remote_file_transfer_cancel` — start a session, `call_json("remote_file_transfer_cancel", transfer_id=tid)`, assert `cancelled:true`
  - (e) `test_transfer_tools_in_expected_set` — update `EXPECTED_TOOLS` to include `"remote_file_transfer_start"`, `"remote_file_transfer_block"`, `"remote_file_transfer_end"`, `"remote_file_transfer_cancel"`; run `test_registered_tool_names` — fails because tools not yet registered.
  - Run: `python -m unittest tests.test_server.TestTransferTools -v` — all fail.
  - Files: `tests/test_server.py`
  - Depends on: 2.12 (ops registered and working)
  - Verify: `python -m unittest tests.test_server.TestTransferTools -v` (5 tests fail)

- [ ] 3.2 **GREEN**: Add four controller-side helper functions in `src/cliptunnel_mcp/server.py` (following the `fs_bin_read`/`fs_bin_write` pattern):
  - `file_transfer_start(filename, direction, size, block_size, checksum, remote_id=None) -> str | None` — calls `controller.send_command_sync(json.dumps({"op":"file.transfer.start",...}))`
  - `file_transfer_block(transfer_id, block_num, data=None, remote_id=None) -> str | None` — calls `controller.send_command_sync(json.dumps({"op":"file.transfer.block",...}))` (include `data` only if not None)
  - `file_transfer_end(transfer_id, remote_id=None) -> str | None` — calls `controller.send_command_sync(json.dumps({"op":"file.transfer.end",...}))`
  - `file_transfer_cancel(transfer_id, remote_id=None) -> str | None` — calls `controller.send_command_sync(json.dumps({"op":"file.transfer.cancel",...}))`
  - Add four `@mcp.tool()` decorators in `create_server()` mirroring the design's MCP tool signatures, each calling `_send(helper, ...)` and returning the JSON string
  - Update `EXPECTED_TOOLS` in `tests/test_server.py` with the four new tool names
  - Run: `python -m unittest tests.test_server.TestTransferTools -v` — all pass.
  - Files: `src/cliptunnel_mcp/server.py`, `tests/test_server.py` (EXPECTED_TOOLS only — already updated in 3.1)
  - Depends on: 3.1
  - Verify: `python -m unittest tests.test_server.TestTransferTools -v` (all pass)

- [ ] 3.3 **RED**: Write failing tests in `tests/test_server.py` for refactored `upload()` and `download()`. New test class `TestBlockUploadDownload(ServerTestCase)`:
  - (a) `test_upload_multi_block` — create a 200KB local file (block_size default 64KB → 4 blocks), `call("remote_upload", local_path=local, remote_path=remote)`, assert response contains `"verified":true` or `"status":"ok"`; verify remote file exists with correct content; verify SHA-256 matches
  - (b) `test_upload_single_block` — create a 32KB file (< 64KB), upload, assert `total_blocks == 1`; verify file content
  - (c) `test_download_multi_block` — create a 200KB remote file (write directly), `call("remote_download", remote_path=remote, local_path=local)`, assert success; verify local file content matches; verify SHA-256
  - (d) `test_download_single_block` — 32KB remote file, download, assert `total_blocks == 1`; verify content
  - (e) `test_upload_checksum_mismatch_reported` — mock/simulate a corrupted block (e.g., send wrong data via individual tools), call `remote_file_transfer_end`, assert `verified:false`; `remote_upload` returns failure message
  - (f) `test_upload_block_error_aborts` — if a block op returns an error, `upload()` aborts and returns the error (test by corrupting the transfer_id mid-stream or sending invalid block_num)
  - (g) `test_upload_download_progress` — call `remote_file_transfer_block` individually, assert response includes `block_num` and `total_blocks` fields
  - (h) `test_download_checksum_provided_by_caller` — caller provides expected checksum at start; after assembly, controller verifies; test with correct checksum → success; test with wrong checksum → failure message
  - Run: `python -m unittest tests.test_server.TestBlockUploadDownload -v` — all fail (upload/download still single-shot).
  - Files: `tests/test_server.py`
  - Depends on: 3.2 (MCP tools work for manual orchestration in tests)
  - Verify: `python -m unittest tests.test_server.TestBlockUploadDownload -v` (8 tests fail)

- [ ] 3.4 **GREEN**: Refactor `upload()` in `src/cliptunnel_mcp/server.py` to use the block transfer protocol:
  - Read local file, compute SHA-256 checksum, resolve block_size via `config.get_env("CLIPTUNNEL_BLOCK_SIZE", default="65536")`
  - Call `file_transfer_start(remote_path, "upload", size, block_size, checksum, remote_id)` → parse `transfer_id`, `total_blocks`
  - Loop `range(total_blocks)`: slice `data[offset:offset+block_size]`, base64-encode, call `file_transfer_block(transfer_id, block_num, b64, remote_id)` → parse response; if `is_error` → call `file_transfer_cancel(transfer_id, remote_id)` and return error
  - Call `file_transfer_end(transfer_id, remote_id)` → parse `verified`; if `verified` → return success JSON; else return `json.dumps({"status":"checksum_failed","path":remote_path})`
  - Run: `python -m unittest tests.test_server.TestBlockUploadDownload -v` — upload tests pass, download tests still fail.
  - Files: `src/cliptunnel_mcp/server.py`
  - Depends on: 3.3
  - Verify: `python -m unittest tests.test_server.TestBlockUploadDownload -v` (upload tests pass)

- [ ] 3.5 **GREEN**: Refactor `download()` in `src/cliptunnel_mcp/server.py` to use the block transfer protocol:
  - Caller provides `remote_path` and `local_path`; for v1, the download `size` and `checksum` are discovered by the agent at `file.transfer.start` (agent computes real size via `os.path.getsize()`, returns `total_blocks` from real size; controller's `size` field is a hint, ignored if mismatched). The `checksum` is provided by the MCP caller (v1 design decision).
  - Call `file_transfer_start(remote_path, "download", size=0, block_size, checksum, remote_id)` → parse `transfer_id`, `total_blocks` (from agent's real computation)
  - Loop `range(total_blocks)`: call `file_transfer_block(transfer_id, block_num, data=None, remote_id)` → parse base64 `data` from response; write decoded bytes to local file at correct offset; if `is_error` → cancel and return error
  - Call `file_transfer_end(transfer_id, remote_id)` → parse response
  - Compute SHA-256 of local file; compare to caller-provided `checksum`; if match → return success JSON; else → return `json.dumps({"status":"checksum_failed","path":local_path})`
  - Since `download()` signature is `(remote_path, local_path, remote_id)` and doesn't include `checksum`, add an optional `checksum: str | None = None` parameter. If `checksum` is `None`, skip verification (return success without checksum check). If provided, verify.
  - Run: `python -m unittest tests.test_server.TestBlockUploadDownload -v` — all pass.
  - Files: `src/cliptunnel_mcp/server.py`
  - Depends on: 3.4
  - Verify: `python -m unittest tests.test_server.TestBlockUploadDownload -v` (all pass)

- [ ] 3.6 **RED**: Write failing tests in `tests/test_server.py` for MCP tool cancel at any point. New test class `TestTransferCancelMCP(ServerTestCase)`:
  - (a) `test_cancel_after_start_before_blocks` — start a session, immediately cancel via `remote_file_transfer_cancel`, assert `cancelled:true`; subsequent `remote_file_transfer_block` returns error
  - (b) `test_cancel_mid_blocks` — start a session, send 3 blocks, cancel, assert `cancelled:true`; send another block → error "invalid or expired"
  - (c) `test_cancel_after_last_block_before_end` — start, send all blocks, cancel (before end), assert `cancelled:true`; call end → error "invalid or expired"
  - Run: `python -m unittest tests.test_server.TestTransferCancelMCP -v` — may pass already if 3.2 is correct; if not, fix.
  - Files: `tests/test_server.py`
  - Depends on: 3.5
  - Verify: `python -m unittest tests.test_server.TestTransferCancelMCP -v` (3 tests — pass or fix)

- [ ] 3.7 **GREEN**: Fix any issues found by 3.6 cancel tests. Ensure `remote_file_transfer_cancel` MCP tool correctly delegates to `file_transfer_cancel` helper and that the agent-side `op_file_transfer_cancel` cleans up at any state. Run: `python -m unittest tests.test_server.TestTransferCancelMCP -v` — all pass.
  - Files: `src/cliptunnel_mcp/server.py` (if fixes needed)
  - Depends on: 3.6
  - Verify: `python -m unittest tests.test_server.TestTransferCancelMCP -v` (all pass)

## Phase 4: Registration — Plugin Registry and ToolSpec Entries

- [ ] 4.1 **RED**: Write failing tests in `tests/test_plugins.py` for the four new ToolSpec entries. Test that `registry.tool_names()` includes `"remote_file_transfer_start"`, `"remote_file_transfer_block"`, `"remote_file_transfer_end"`, `"remote_file_transfer_cancel"`. Test that each `ToolSpec` has correct `name`, `description`, `input_schema`, and `handler`. Run: `python -m unittest tests.test_plugins -v` — fail (tools not registered in plugins).
  - Files: `tests/test_plugins.py`
  - Depends on: 3.2 (server helpers exist)
  - Verify: `python -m unittest tests.test_plugins -v` (new tests fail)

- [ ] 4.2 **GREEN**: Add four `ToolSpec` entries to `_register_server_tools()` in `src/cliptunnel_mcp/plugins.py`, following the exact pattern of existing entries (e.g., `remote_fs_bin_read`). Each entry maps the tool name to the server helper function with an appropriate `input_schema`:
  - `("remote_file_transfer_start", server.file_transfer_start, "Start a block-based file transfer session on the remote machine.", {filename, direction, size, block_size, checksum, remote_id})`
  - `("remote_file_transfer_block", server.file_transfer_block, "Send or receive a single block in an active transfer session.", {transfer_id, block_num, data, remote_id})`
  - `("remote_file_transfer_end", server.file_transfer_end, "Finalize a transfer: verify checksum and commit the file.", {transfer_id, remote_id})`
  - `("remote_file_transfer_cancel", server.file_transfer_cancel, "Cancel an active transfer and clean up temp files.", {transfer_id, remote_id})`
  - Run: `python -m unittest tests.test_plugins -v` — all pass.
  - Files: `src/cliptunnel_mcp/plugins.py`
  - Depends on: 4.1
  - Verify: `python -m unittest tests.test_plugins -v` (all pass)
  - Commit: `feat(plugins): register file transfer MCP tools as ToolSpec entries in ExtensionRegistry`

## Phase 5: Full Verification and Cleanup

- [ ] 5.1 Run the complete test suite to verify no regressions: `python -m unittest discover -s tests -t . -v`. All tests must pass — existing tests (shell, fs, agent, transport, protocol, etc.) and all new tests (transfer_session, operations transfer ops, server transfer tools, plugins).
  - Files: no file changes
  - Depends on: 4.2
  - Verify: `python -m unittest discover -s tests -t . -v` (zero failures)

- [ ] 5.2 **REFACTOR**: Review the implementation for shared block-loop logic between `upload()` and `download()` in `server.py`. If there is duplicated block-iteration code, extract a shared helper (e.g., `_block_transfer_loop(transfer_id, total_blocks, remote_id, block_sender)`) to reduce duplication. Ensure the refactor does not break any tests. Run: `python -m unittest discover -s tests -t . -v` — all pass.
  - Files: `src/cliptunnel_mcp/server.py`
  - Depends on: 5.1
  - Verify: `python -m unittest discover -s tests -t . -v` (zero failures)
  - Commit: `refactor(transfer): extract shared block-loop helper in upload/download`

- [ ] 5.3 Verify backward compatibility explicitly: confirm `fs.bin_write` / `fs.bin_read` ops and `remote_fs_bin_write` / `remote_fs_bin_read` MCP tools work unchanged alongside the new block transfer ops. The existing `test_upload_download_roundtrip` test in `TestFsTools` must still pass (it tests the old single-shot path via `fs.bin_write`/`fs.bin_read`). If `upload()`/`download()` refactor broke this test, update it to use the new block protocol path while keeping the `fs.bin_write`/`fs.bin_read` direct tests intact.
  - Files: `tests/test_server.py` (if the existing roundtrip test needs adjustment for the refactored upload/download)
  - Depends on: 5.2
  - Verify: `python -m unittest tests.test_server.TestFsTools -v` (all pass)

- [ ] 5.4 Verify the `TestToolSurface.test_registered_tool_names` test passes with the updated `EXPECTED_TOOLS` set. All 31 tools (27 existing + 4 new) must be registered.
  - Files: no changes (verification only)
  - Depends on: 5.3
  - Verify: `python -m unittest tests.test_server.TestToolSurface -v` (all pass)

- [ ] 5.5 **Final commit**: If any remaining changes from refactor or test adjustments, commit as: `feat(transfer): complete block-based file transfer with upload/download refactor and full test coverage`. Run final verification: `python -m unittest discover -s tests -t . -v` — zero failures.
  - Files: any remaining
  - Depends on: 5.4
  - Verify: `python -m unittest discover -s tests -t . -v` (zero failures)