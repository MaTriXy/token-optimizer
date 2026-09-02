"""Cursor installer: read-merge-write of ~/.cursor/hooks.json + Windows refusal.

Unlike Copilot (which owns a dedicated per-plugin hook file), Cursor has ONE
shared ``~/.cursor/hooks.json``. The installer must therefore merge, never
clobber: foreign entries are preserved verbatim, only entries whose ``command``
points at our bridge path are replaced/removed. These tests pin that contract,
the six-event hook map, and the fail-closed Windows refusal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cursor_install as ci  # noqa: E402


@pytest.fixture()
def home(tmp_path):
    return tmp_path / "cursor-home"


def _bridge_path(home):
    return ci._plugin_dir(home) / "cursor_hook_bridge.py"


def _foreign_entry():
    return {"command": "echo other-tool", "type": "command", "timeout": 5}


def test_install_writes_merged_hooks_and_payload(home):
    home.mkdir()
    # A foreign entry in sessionStart must survive; a stale "ours" entry is replaced.
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text(json.dumps({
        "version": 1,
        "hooks": {
            "sessionStart": [
                _foreign_entry(),
                {"command": f"TOKEN_OPTIMIZER_RUNTIME=cursor python3 {_bridge_path(home)} sessionStart"},
            ],
        },
    }), encoding="utf-8")

    result = ci.install(home=home)

    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert result["hook_file"] == str(hooks_path)
    # every wired event is present
    assert set(data["hooks"]) == {
        "sessionStart", "preToolUse", "postToolUse", "preCompact", "stop", "sessionEnd"
    }
    # foreign entry preserved, stale ours replaced by exactly one fresh ours
    starts = data["hooks"]["sessionStart"]
    assert _foreign_entry() in starts
    ours = [e for e in starts if str(_bridge_path(home)) in e["command"]]
    assert len(ours) == 1
    assert " python3 " not in f" {ours[0]['command']} "

    # payload landed next to the bridge path the command points at
    assert _bridge_path(home).exists()


def test_install_never_drops_foreign_entries(home):
    home.mkdir()
    ci._host_hooks_path(home).write_text(json.dumps({
        "version": 1,
        "hooks": {"stop": [_foreign_entry()]},
    }), encoding="utf-8")

    ci.install(home=home)
    ci.install(home=home)  # idempotent

    data = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    stops = data["hooks"]["stop"]
    assert _foreign_entry() in stops
    assert len([e for e in stops if str(_bridge_path(home)) in e["command"]]) == 1


def test_uninstall_removes_only_our_entries_and_payload(home):
    home.mkdir()
    ci._host_hooks_path(home).write_text(json.dumps({
        "version": 1,
        "hooks": {"sessionStart": [_foreign_entry()]},
    }), encoding="utf-8")
    ci.install(home=home)

    result = ci.uninstall(home=home)

    data = json.loads(ci._host_hooks_path(home).read_text(encoding="utf-8"))
    assert data["hooks"]["sessionStart"] == [_foreign_entry()]
    assert ci._plugin_dir(home).exists() is False
    assert any("sessionStart" in (r or "") for r in result["removed"]) or len(result["removed"]) >= 1


def test_dry_run_writes_nothing(home):
    home.mkdir()
    result = ci.install(dry_run=True, home=home)
    assert result["dry_run"] is True
    assert ci._host_hooks_path(home).exists() is False
    assert ci._plugin_dir(home).exists() is False


def test_windows_install_refuses(monkeypatch, home):
    home.mkdir()
    monkeypatch.setattr(ci.os, "name", "nt")
    with pytest.raises(RuntimeError, match="Windows"):
        ci.install(home=home)


def test_is_ours_matches_bridge_path(home):
    bridge_path = _bridge_path(home)
    assert ci._is_ours({"command": f"python3 {bridge_path} stop"}, bridge_path) is True
    assert ci._is_ours(_foreign_entry(), bridge_path) is False
    assert ci._is_ours("not-a-dict", bridge_path) is False


def test_install_writes_measure_locator(home):
    home.mkdir()
    ci.install(home=home)

    locator = ci._plugin_dir(home) / ci._MEASURE_LOCATOR_NAME
    assert locator.exists()
    target = Path(locator.read_text(encoding="utf-8").strip())
    assert target.is_file() and target.name == "measure.py"


def test_install_aborts_on_corrupt_hooks_json(home):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not valid JSON"):
        ci.install(home=home)
    # The shared file must be left byte-for-byte intact, never clobbered.
    assert hooks_path.read_text(encoding="utf-8") == "{ not valid json"


def test_install_aborts_on_non_object_hooks_root(home):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="non-object root"):
        ci.install(home=home)
    assert hooks_path.read_text(encoding="utf-8") == "[1, 2, 3]"


def test_install_aborts_on_copy_failure_before_touching_hooks(home, monkeypatch):
    home.mkdir()
    hooks_path = ci._host_hooks_path(home)
    hooks_path.write_text(json.dumps({"version": 1, "hooks": {"stop": [_foreign_entry()]}}),
                          encoding="utf-8")

    def _boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(ci.shutil, "copy2", _boom)
    with pytest.raises(RuntimeError, match="failed copying"):
        ci.install(home=home)
    # Abort happened before the read-merge-write: foreign entries untouched.
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert data["hooks"]["stop"] == [_foreign_entry()]


def test_install_aborts_on_missing_payload_module(home, monkeypatch):
    home.mkdir()
    monkeypatch.setattr(ci, "_PAYLOAD_MODULES", ("cursor_hook_bridge.py", "nonexistent.py"))
    with pytest.raises(RuntimeError, match="missing payload modules"):
        ci.install(home=home)
    assert ci._host_hooks_path(home).exists() is False
