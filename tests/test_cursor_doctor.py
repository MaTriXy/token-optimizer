"""Cursor doctor: hook-config recognition, payload/observed checks, and --probe.

The probe is the live-firing substitute for a headless Cursor: it replays the
documented payload for every wired event through the exact installed command
string under /bin/sh -c and asserts exit 0. Everything else is pinned without a
live Cursor by monkeypatching the home dir.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_doctor as cd  # noqa: E402

_WIRED = cd._WIRED_EVENTS


@pytest.fixture()
def cur(tmp_path):
    d = tmp_path / ".cursor"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_hooks(cur, *, include_ours=True):
    hooks = {}
    if include_ours:
        bridge = SCRIPTS / "cursor_hook_bridge.py"
        py = sys.executable
        for event in _WIRED:
            hooks[event] = [{
                "command": f"TOKEN_OPTIMIZER_RUNTIME=cursor {py} {bridge} {event}",
                "type": "command",
                "timeout": 10,
            }]
    (cur / "hooks.json").write_text(json.dumps({"version": 1, "hooks": hooks}), encoding="utf-8")


def test_hook_config_recognizes_wired_events(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur)
    checks = cd._hook_config_checks()
    assert any(c["name"] == "TO hook config" and c["status"] == "ok" for c in checks)
    # no missing-event warning
    assert not any(c["name"] == "hook event coverage" for c in checks)


def test_hook_config_reports_missing_when_uninstalled(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur, include_ours=False)
    checks = cd._hook_config_checks()
    assert any(c["name"] == "TO hook config" and c["status"] == "fail" for c in checks)


def test_payload_checks(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    plugin = cur / "token-optimizer" / "plugin"
    plugin.mkdir(parents=True)
    for name in ("cursor_hook_bridge.py", "codex_io.py", "bash_compress.py"):
        (plugin / name).write_text("# stub\n")
    checks = cd._payload_checks()
    assert any(c["name"] == "hook payload" and c["status"] == "ok" for c in checks)


def test_observed_checks_counts_rewrite_statuses(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    to_dir = cur / "token-optimizer"
    to_dir.mkdir(parents=True)
    (to_dir / "observed-events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            {"event": "preToolUse", "rewrite": "attempted", "cursor_version": "3.18.9"},
            {"event": "postToolUse", "rewrite": "honoured", "cursor_version": "3.18.9"},
            {"event": "postToolUse", "rewrite": "ignored", "cursor_version": "3.18.9"},
        ]) + "\n",
        encoding="utf-8",
    )
    checks = cd._observed_checks()
    detail = next(c["detail"] for c in checks if c["name"] == "observed events")
    assert "1 honoured, 1 ignored" in detail


def test_run_checks_includes_daemon_and_data(monkeypatch, cur):
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)
    _write_hooks(cur)
    names = {c["name"] for c in cd.run_checks()}
    assert "dashboard daemon" in names
    assert "IDE token plane" in names
    assert "CLI transcript plane" in names


@pytest.mark.skipif(sys.platform == "win32", reason="probe is POSIX-only")
def test_probe_fires_all_installed_events(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CURSOR_HOME", str(cur))
    _write_hooks(cur)

    results = cd.run_probe()

    assert [r["event"] for r in results] == list(_WIRED)
    assert all(r["status"] == "ok" for r in results), results
    # The probe genuinely ran the bridge, but its writes are isolated to a
    # throwaway cursor home: replaying documented payloads must prove the hooks
    # can fire WITHOUT contaminating real session data with synthetic rows.
    ledger = cur / "token-optimizer" / "observed-events.jsonl"
    assert not ledger.exists()
    assert not (cur / "token-optimizer" / "sessions").exists()


def test_parse_hook_command_accepts_installed_shape():
    cmd = f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} /x/cursor_hook_bridge.py stop"
    assert cd._parse_hook_command(cmd) == [sys.executable, "/x/cursor_hook_bridge.py", "stop"]


@pytest.mark.parametrize("cmd", [
    # shell injection via metacharacters: shlex keeps it one token -> shape fails
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /tmp/cursor_hook_bridge.py preToolUse; echo INJECTED > /tmp/pwned",
    # extra token
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py stop extra",
    # relative python
    "TOKEN_OPTIMIZER_RUNTIME=cursor python3 /x/bridge.py stop",
    # unknown event
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py notAnEvent",
    # wrong runtime pin
    "TOKEN_OPTIMIZER_RUNTIME=codex /usr/bin/python3 /x/bridge.py stop",
    # unbalanced quote (shlex ValueError)
    "TOKEN_OPTIMIZER_RUNTIME=cursor /usr/bin/python3 /x/bridge.py 'stop",
])
def test_parse_hook_command_rejects_malformed(cmd):
    assert cd._parse_hook_command(cmd) is None


@pytest.mark.skipif(sys.platform == "win32", reason="probe is POSIX-only")
def test_probe_refuses_to_execute_injected_command(monkeypatch, tmp_path):
    """P0-2: a corrupted/malicious hooks.json entry must be reported, never run."""
    home = tmp_path / "home"
    cur = home / ".cursor"
    cur.mkdir(parents=True)
    marker = tmp_path / "pwned"
    (cur / "hooks.json").write_text(json.dumps({"hooks": {
        "stop": [{"command":
                  f"TOKEN_OPTIMIZER_RUNTIME=cursor {sys.executable} /tmp/cursor_hook_bridge.py stop; echo INJECTED > {marker}"}],
    }}), encoding="utf-8")
    monkeypatch.setattr(cd, "cursor_home", lambda: cur)

    results = cd.run_probe()

    stop = next(r for r in results if r["event"] == "stop")
    # The entry is not ours (bridge path is /tmp/...), so it is never replayed:
    # reported as a failure or a skip, but never executed.
    assert stop["status"] in ("fail", "skip")
    assert not marker.exists()
