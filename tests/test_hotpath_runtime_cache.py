#!/usr/bin/env python3
"""Hot-path latency tests for the shared process snapshot + negative-result
ancestor-scan cache (Track F lever 2) and the run.py runtime export.

What changed:
- runtime_env._load_proc_snapshot(): ONE `ps -Ao pid=,ppid=,comm=,args=`
  snapshot per process, shared by the OpenCode (args) and Copilot (comm)
  ancestor scanners, which previously spawned one `ps` each.
- runtime_env._opencode_in_process_tree(): persists a NEGATIVE scan result to
  a short-TTL disk cache keyed by parent pid + runtime-signal env signature so
  subsequent hook processes skip the scan. Only negatives are cached, so a
  stale entry can never flip a session INTO another runtime.
- hooks/run.py: exports detect_runtime()'s resolution to the dispatched child
  via TOKEN_OPTIMIZER_RUNTIME so the child (whose parent is the ephemeral
  dispatcher, making its own cache useless) never re-scans.

Contracts guarded here:
1. Snapshot parse correctness for both scanners (comm vs args columns).
2. Negative result is cached; a cache hit spawns no `ps`.
3. A POSITIVE finding is never cached (re-derived live every scan).
4. Cache is invalidated by a different env signature.
5. Expired entries (TTL) are ignored.
6. TOKEN_OPTIMIZER_NO_PROC_SCAN disables both the scan and cache writes.
7. detect_runtime() honors an exported TOKEN_OPTIMIZER_RUNTIME without any
   process scan (the run.py child contract).
8. A failing `ps` behaves as "no ancestor found" and never raises.

Run directly:  python3 tests/test_hotpath_runtime_cache.py
Exits non-zero on first failure.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

# Every env var the ancestor cache key hashes — cleared before each case.
_CONTROLLED_ENV = (
    "TOKEN_OPTIMIZER_RUNTIME",
    "CLAUDE_PLUGIN_ROOT",
    "CLAUDE_PLUGIN_DATA",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_HOME",
    "HERMES_HOME",
    "COPILOT_HOME",
    "TOKEN_OPTIMIZER_COPILOT_HOME",
    "TOKEN_OPTIMIZER_NO_PROC_SCAN",
    "OPENCODE_BIN",
    "OPENCODE_CONFIG_DIR",
    "OPENCODE_DATA_DIR",
    "OPENCODE_CONFIG",
    "OPENCODE_CLIENT",
    "XDG_CACHE_HOME",
)

# Fake process tree used as the ps output: the walk starts at FAKE_PID.
_FAKE_PID = 5000
_FAKE_PARENT = 4000
_FAKE_GRANDPARENT = 3000


def _ps_table(*rows):
    """Build ps output; each row is (pid, ppid, comm, args).

    A row for the walked pid (_FAKE_PID) is always included so the parent
    chain exists; pass its (pid, ppid, comm, args) as the first row.
    """
    base = [(1, 0, "/sbin/launchd", "/sbin/launchd")]
    return "\n".join(
        f"{pid:>6} {ppid:>6} {comm} {args}".rstrip() for pid, ppid, comm, args in [*base, *rows]
    ) + "\n"


def _hook_chain(parent_comm="bash", parent_args="/bin/bash"):
    """Rows for the walked pid and its parent (no opencode/copilot ancestor)."""
    return [
        (_FAKE_PID, _FAKE_PARENT, "python3", "python3 run.py"),
        (_FAKE_PARENT, 1, parent_comm, parent_args),
    ]


class _FakeProc:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


class _Controlled:
    """Clean signal env + isolated cache dir + fresh runtime_env state."""

    def __init__(self, env=None):
        self.env = env or {}

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k: os.environ.get(k) for k in _CONTROLLED_ENV}
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        os.environ["XDG_CACHE_HOME"] = str(Path(self.tmp.name) / "cache")
        for k, v in self.env.items():
            os.environ[k] = v
        runtime_env._PROC_SCAN_SNAPSHOT = None
        runtime_env.detect_runtime.cache_clear()
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        runtime_env._PROC_SCAN_SNAPSHOT = None
        runtime_env.detect_runtime.cache_clear()
        self.tmp.cleanup()

    def cache_file(self):
        return Path(self.tmp.name) / "cache" / "token-optimizer" / "ancestor-scan.json"


def _patch_ps(table="", returncode=0):
    """Patch subprocess.run (the local import inside _load_proc_snapshot
    resolves the real module) and os.getpid (the walk starts there)."""
    calls = {"n": 0}

    def fake_run(*args, **kwargs):
        calls["n"] += 1
        return _FakeProc(table, returncode)

    p1 = mock.patch("subprocess.run", side_effect=fake_run)
    p2 = mock.patch.object(runtime_env.os, "getpid", return_value=_FAKE_PID)
    p1.start()
    p2.start()
    return calls, (p1, p2)


def _stop(patch_pair):
    for p in patch_pair:
        p.stop()


def test_snapshot_shared_by_both_scanners():
    """One ps spawn serves both the OpenCode (args) and Copilot (comm) scans."""
    with _Controlled():
        table = _ps_table(
            (_FAKE_PID, _FAKE_PARENT, "python3", "python3 run.py"),
            (_FAKE_PARENT, _FAKE_GRANDPARENT, "bash", "/bin/bash -c echo hi"),
            (_FAKE_GRANDPARENT, 1, "zsh", "-l"),
        )
        calls, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            assert runtime_env._ancestor_in_process_tree(frozenset({"zsh"})) is True
            assert runtime_env._ancestor_in_process_tree(frozenset({"copilot"})) is False
            assert calls["n"] == 1, f"expected 1 ps spawn, got {calls['n']}"
        finally:
            _stop(patches)


def test_opencode_ancestor_found_via_args_column():
    """OpenCode launched through node is recognized from the args column."""
    with _Controlled():
        table = _ps_table(
            (_FAKE_PID, _FAKE_PARENT, "python3", "python3 run.py"),
            (_FAKE_PARENT, 1, "node", "node /opt/x/node_modules/opencode-ai/dist/index.js"),
        )
        _, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is True
        finally:
            _stop(patches)


def test_negative_scan_is_cached_and_skips_ps():
    """A negative OpenCode scan writes the cache; the next process skips ps."""
    with _Controlled() as ctx:
        table = _ps_table(*_hook_chain())
        calls, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            assert calls["n"] == 1
        finally:
            _stop(patches)
        assert ctx.cache_file().is_file(), "negative result not persisted"
        # Fresh process state (new snapshot) — the cache must answer alone.
        runtime_env._PROC_SCAN_SNAPSHOT = None
        calls2, patches2 = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            assert calls2["n"] == 0, "cache hit must not spawn ps"
        finally:
            _stop(patches2)


def test_positive_finding_is_never_cached():
    """An opencode ancestor is re-derived live; nothing is persisted."""
    with _Controlled() as ctx:
        table = _ps_table(
            (_FAKE_PID, _FAKE_PARENT, "python3", "python3 run.py"),
            (_FAKE_PARENT, 1, "node", "node /opt/x/node_modules/opencode-ai/dist/index.js"),
        )
        _, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is True
        finally:
            _stop(patches)
        assert not ctx.cache_file().exists(), "positive finding must not be cached"


def test_cache_invalidated_by_env_signature():
    """A different runtime-signal env must not consume another env's negative."""
    with _Controlled():
        table = _ps_table(*_hook_chain())
        calls, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            runtime_env._PROC_SCAN_SNAPSHOT = None
            os.environ["CLAUDECODE"] = "1"  # different signal env -> different key
            assert runtime_env._opencode_in_process_tree() is False
            assert calls["n"] == 2, "changed env signature must re-scan"
        finally:
            _stop(patches)


def test_expired_cache_entry_is_ignored():
    with _Controlled() as ctx:
        table = _ps_table(*_hook_chain())
        calls, patches = _patch_ps(table)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            cache = ctx.cache_file()
            entry = json.loads(cache.read_text())
            entry["ts"] = time.time() - (runtime_env._ANCESTOR_CACHE_TTL_SECONDS + 5)
            cache.write_text(json.dumps(entry))
            runtime_env._PROC_SCAN_SNAPSHOT = None
            assert runtime_env._opencode_in_process_tree() is False
            assert calls["n"] == 2, "expired entry must re-scan"
        finally:
            _stop(patches)


def test_no_proc_scan_disables_scan_and_cache_write():
    with _Controlled(env={"TOKEN_OPTIMIZER_NO_PROC_SCAN": "1"}) as ctx:
        calls, patches = _patch_ps()
        try:
            assert runtime_env._opencode_in_process_tree() is False
            assert calls["n"] == 0
            assert not ctx.cache_file().exists(), "disabled scan must not write cache"
        finally:
            _stop(patches)


def test_exported_runtime_override_skips_scan():
    """run.py child contract: TOKEN_OPTIMIZER_RUNTIME resolves with zero ps."""
    with _Controlled(env={"TOKEN_OPTIMIZER_RUNTIME": "claude"}):
        calls, patches = _patch_ps()
        try:
            assert runtime_env.detect_runtime() == "claude"
            assert calls["n"] == 0, "override tier must resolve before any scan"
        finally:
            _stop(patches)


def test_ps_failure_behaves_as_no_ancestor():
    """A failing ps yields empty tables: no ancestor found, no crash."""
    with _Controlled():
        _, patches = _patch_ps("", returncode=1)
        try:
            assert runtime_env._opencode_in_process_tree() is False
            assert runtime_env._ancestor_in_process_tree(frozenset({"bash"})) is False
        finally:
            _stop(patches)


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
