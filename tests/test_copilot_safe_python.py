"""Copilot hook must never persist a PATH-hijackable bare `python3`.

copilot_install wrote `sys.executable or "python3"` into the persisted hook
command; when sys.executable was empty the literal "python3" was resolved via
$PATH every time the hook fired -- the exact hijack the launcher's allowlist
exists to stop, and the Copilot bridge does not use the launcher. The resolver
must emit an ABSOLUTE, trusted path or fail, never a bare name.

These tests are ENVIRONMENT-INDEPENDENT on purpose: an earlier revision assumed
sys.executable's basename and ownership (true on a dev Mac, false on CI where
Python is root-owned hostedtoolcache), which is exactly the kind of assumption
that passes locally and breaks CI. Here we build controlled files instead.

Run: python3 -m pytest tests/test_copilot_safe_python.py -v
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
    sys.modules.pop("copilot_install", None)
    yield importlib.import_module("copilot_install")


def test_resolver_returns_absolute_existing_file(c):
    """Whatever it returns must be an absolute path to a real file -- never a bare
    name that $PATH would resolve at hook time. (An absolute path may legitimately
    be named .../bin/python, so we do NOT reject that.)"""
    r = c._resolve_safe_python()
    assert os.path.isabs(r), f"not absolute: {r}"
    assert os.path.isfile(r), f"not a real file: {r}"


def test_hook_command_bakes_the_resolved_absolute_path(c, tmp_path):
    resolved = c._resolve_safe_python()
    cmd = c._hook_config(tmp_path / "copilot_hook_bridge.py")["hooks"]["preToolUse"][0]["bash"]
    # the resolved absolute path is what lands in the persisted command...
    assert resolved in cmd, f"resolved path not in hook command: {cmd}"
    # ...and there is no space-delimited bare `python3`/`python` token.
    assert " python3 " not in f" {cmd} " and " python " not in f" {cmd} ", cmd


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
    """A user-owned interpreter in a user-owned, not-other-writable dir is
    trusted -- the version-manager-shim case, built controlled so it holds on any
    host regardless of how the CI Python itself is installed."""
    d = tempfile.mkdtemp()
    os.chmod(d, 0o755)
    f = os.path.join(d, "python3")
    open(f, "w").close()
    os.chmod(f, 0o755)
    assert c._py_path_is_trusted(f) is True


def test_trust_gate_accepts_system_prefix(c):
    """Root-owned system installs are trusted by prefix (they are not user-
    writable), so the fallback never rejects a legitimate /usr/bin/python3."""
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3",
              "/opt/hostedtoolcache/Python/3.12/x64/bin/python"):
        # only assert on prefixes that actually resolve to a file on this host;
        # the point is the prefix logic, not that every path exists everywhere.
        if os.path.isfile(p):
            assert c._py_path_is_trusted(p) is True, p


def test_override_env_is_honored_when_trusted(c, monkeypatch):
    resolved = c._resolve_safe_python()  # a known-trusted absolute path on this host
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", resolved)
    assert c._resolve_safe_python() == os.path.abspath(resolved)
    # a bogus override is ignored; resolver still returns a real file
    monkeypatch.setenv("TOKEN_OPTIMIZER_PYTHON", "/nonexistent/python3")
    assert os.path.isfile(c._resolve_safe_python())


@pytest.mark.skipif(not hasattr(os, "geteuid"), reason="POSIX ownership test")
def test_sys_executable_must_pass_the_trust_gate(c, monkeypatch):
    """P0-1 (same bug as cursor_install): sys.executable is only persisted when
    the trust gate accepts it. A writable venv interpreter (gate rejects) must
    fall through to the $PATH search, never be persisted as-is."""
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
