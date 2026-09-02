"""Cursor hook must never persist a PATH-hijackable bare `python3`.

Clone of test_copilot_safe_python.py against cursor_install: the resolver must
emit an ABSOLUTE, trusted path or fail, never a bare name that $PATH would
resolve at hook time. Environment-independent by construction (controlled files,
not assumptions about the host interpreter's ownership).
"""

import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def c():
    sys.path.insert(0, str(SCRIPTS))
    sys.modules.pop("cursor_install", None)
    yield importlib.import_module("cursor_install")


def test_resolver_returns_absolute_existing_file(c):
    r = c._resolve_safe_python()
    assert os.path.isabs(r), f"not absolute: {r}"
    assert os.path.isfile(r), f"not a real file: {r}"


def test_hook_command_bakes_the_resolved_absolute_path(c, tmp_path):
    resolved = c._resolve_safe_python()
    entries = c._hook_entries(tmp_path / "cursor_hook_bridge.py")
    cmd = entries["preToolUse"][0]["command"]
    assert resolved in cmd, f"resolved path not in hook command: {cmd}"
    assert " python3 " not in f" {cmd} " and " python " not in f" {cmd} ", cmd


def test_hook_entries_cover_all_six_events(c, tmp_path):
    entries = c._hook_entries(tmp_path / "cursor_hook_bridge.py")
    assert set(entries) == {
        "sessionStart", "preToolUse", "postToolUse", "preCompact", "stop", "sessionEnd"
    }
    # The Shell matcher keeps non-Shell tool calls out of the hot path.
    assert entries["preToolUse"][0]["matcher"] == "Shell"
    assert all(e["timeout"] == c.HOOK_TIMEOUT_SEC for es in entries.values() for e in es)


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_rejects_hijackable_paths(c):
    # world-writable DIR -> anyone can swap the file
    d = tempfile.mkdtemp()
    os.chmod(d, 0o777)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert c._py_path_is_trusted(f) is False
    # world-writable FILE in an owned dir -> anyone can rewrite its bytes
    d2 = tempfile.mkdtemp()
    os.chmod(d2, 0o755)
    f2 = os.path.join(d2, "python3")
    open(f2, "w").close()
    os.chmod(f2, 0o777)
    assert c._py_path_is_trusted(f2) is False


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_trust_gate_accepts_owned_unwritable_file(c):
    d = tempfile.mkdtemp()
    os.chmod(d, 0o755)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert c._py_path_is_trusted(f) is True


def test_trust_gate_accepts_system_prefix(c):
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3",
              "/opt/hostedtoolcache/Python/3.12/x64/bin/python"):
        if os.path.isfile(p):
            assert c._py_path_is_trusted(p) is True, p


def test_override_env_is_honored_when_trusted(c, monkeypatch):
    resolved = c._resolve_safe_python()
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", resolved)
    assert c._resolve_safe_python() == os.path.abspath(resolved)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "/nonexistent/python3")
    assert os.path.isfile(c._resolve_safe_python())


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_sys_executable_must_pass_the_trust_gate(c, monkeypatch):
    """P0-1: sys.executable is only persisted when the trust gate accepts it.
    A writable venv interpreter (gate rejects) must fall through to the $PATH
    search, never be persisted as-is."""
    calls = []

    def fake_gate(p):
        calls.append(p)
        return os.path.abspath(p) != os.path.abspath(sys.executable)

    monkeypatch.setattr(c, "_py_path_is_trusted", fake_gate)
    resolved = c._resolve_safe_python()
    assert sys.executable in calls, "gate never consulted for sys.executable"
    assert os.path.isabs(resolved) and os.path.isfile(resolved)


def test_sys_executable_returned_when_trusted(c, monkeypatch):
    monkeypatch.setattr(c, "_py_path_is_trusted", lambda p: True)
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "")
    assert c._resolve_safe_python() == os.path.abspath(sys.executable)
