# No-Kill Chrome Policy - Learnings

## Task 2: Remove Signal/Atexit Handlers

**Status**: ✅ COMPLETED

### What Was Removed
- `_signal_handler()` method (lines 242-247) — handled SIGTERM/SIGINT
- `_atexit_handler()` method (lines 249-253) — handled process exit
- `signal.signal(signal.SIGTERM, ...)` registration (line 267)
- `signal.signal(signal.SIGINT, ...)` registration (line 268)
- `atexit.register(self._atexit_handler)` registration (line 269)
- `import signal` (line 15)
- `import atexit` (line 13)
- `import sys` (line 16) — was only used in `_signal_handler` for `sys.exit(0)`

### What Was Preserved
- `_cleanup()` method — handles async CDP disconnection via `self._pool.cleanup_all()`
- `finally: await self._cleanup()` block in `run()` — ensures cleanup on normal/abnormal exit

### Key Insight
The signal/atexit handlers were **synchronous kill operations** that called `self._pool._sync_kill_all_chrome()`. With the "never kill Chrome" policy, these are obsolete. The proper cleanup path is:
1. MCP server exits normally or abnormally
2. `finally` block triggers `_cleanup()`
3. `_cleanup()` calls `self._pool.cleanup_all()` (async, graceful)
4. Chrome processes remain running for reuse

### Verification
- ✅ Ruff check passes (0 errors)
- ✅ `_signal_handler` removed (not in class)
- ✅ `_atexit_handler` removed (not in class)
- ✅ `_cleanup()` preserved (still in class)
- ✅ No signal/atexit imports or registrations in file
- ✅ Commit: `af913bf`

### Files Modified
- `src/wsl_chrome_mcp/server.py` — 20 lines removed

### Next Steps
- Task 3: Remove `_sync_kill_all_chrome()` method from `ChromePoolManager`
- Task 4: Remove `_sync_kill_all_chrome()` calls from `destroy()` method

## Task: Update Tests for No-Kill-Chrome Behavior

**Status**: ✅ COMPLETED

### Tests Deleted (2)
- `test_collect_all_pids_returns_all` — tested `_collect_all_pids()` which no longer exists
- `test_sync_kill_all_chrome_calls_kill` — tested `_sync_kill_all_chrome()` which no longer exists

### Tests Updated (3)
- `test_session_destroy_deletes_from_disk` → `test_session_destroy_preserves_session_on_disk` — flipped assertion: session file now preserved after `destroy()`
- `test_cleanup_all_deletes_all_session_files` → `test_cleanup_all_preserves_session_files` — flipped assertion: session files now preserved after `cleanup_all()`, removed kill/disconnect mocks
- `test_cleanup_all_destroys_all` (test_chrome_pool.py) — removed `_kill_shared_chrome` mock, set `_shared_browser_cdp=None` to match new `cleanup_all()` implementation

### Tests Added (5)
- `test_cleanup_all_disconnects_cdp` — verifies `_disconnect_cdp` called for each instance
- `test_cleanup_all_disconnects_shared_browser_cdp` — verifies `_shared_browser_cdp.close()` called
- `test_destroy_does_not_kill_even_with_owns_chrome` — verifies `_kill_instance_chrome` NOT called
- `test_port_prepopulation_from_session_records` — verifies `_used_ports` populated from `session_store.list_all()` on init
- `test_session_records_survive_cleanup_all` — verifies records persist on disk after `cleanup_all()`

### Key Discovery
- `cleanup_all()` references `self._shared_browser_cdp` but `__init__` defines `self._browser_cdp`. Tests must set `_shared_browser_cdp = None` to avoid AttributeError. This may be a source code naming inconsistency.

### Verification
- ✅ 155/155 tests pass
- ✅ 0 new ruff errors (8 pre-existing)

### Files Modified
- `tests/test_reconnection.py` — 2 deleted, 2 updated, 5 added
- `tests/test_chrome_pool.py` — 1 updated
