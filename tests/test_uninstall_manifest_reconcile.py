#!/usr/bin/env python3
"""Uninstall-direction manifest reconcile.

``/plugin uninstall`` removes the plugin cache dir but does NOT clean the
plugin's keys in the user's ``installed_plugins.json`` or
``known_marketplaces.json`` (Claude Code ages orphaned entries out on its own
schedule). The cleanup command reconciles the uninstall direction: REPORT our
stale entries by default, and under the explicit cleanup command REMOVE them
(backup first), leaving other plugins' entries byte-identical.

Run directly:  python3 tests/test_uninstall_manifest_reconcile.py
Exits non-zero on first failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import install_reconcile as ir  # noqa: E402


def _write_installed(plugins_dir: Path, plugins: dict) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / "installed_plugins.json"
    path.write_text(json.dumps({"version": 2, "plugins": plugins}, indent=2), encoding="utf-8")
    return path


def _write_marketplaces(plugins_dir: Path, entries: dict) -> Path:
    plugins_dir.mkdir(parents=True, exist_ok=True)
    path = plugins_dir / "known_marketplaces.json"
    path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    return path


def test_report_only_does_not_modify(tmp_path):
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    cache = plugins_dir / "cache"
    # Our entry: installPath gone (stale). A foreign entry: present (active).
    our_install = cache / "alexgreensh-token-optimizer" / "token-optimizer" / "5.11.67"
    # NOTE: not created -> stale
    foreign_install = cache / "claude-plugins-official" / "ralph-loop" / "1.0.0"
    foreign_install.mkdir(parents=True)
    inst = _write_installed(plugins_dir, {
        "token-optimizer@alexgreensh-token-optimizer": [
            {"installPath": str(our_install), "version": "5.11.67"}
        ],
        "ralph-loop@claude-plugins-official": [
            {"installPath": str(foreign_install), "version": "1.0.0"}
        ],
    })
    mkt = _write_marketplaces(plugins_dir, {
        "alexgreensh-token-optimizer": {"installLocation": str(cache.parent / "marketplaces" / "alexgreensh-token-optimizer")},
        "claude-plugins-official": {"installLocation": str(cache.parent / "marketplaces" / "claude-plugins-official")},
    })
    before_inst = inst.read_text(encoding="utf-8")
    before_mkt = mkt.read_text(encoding="utf-8")

    result = ir.reconcile_uninstall(home, dry_run=False, remove=False)
    # Reports our stale entry + our marketplace; does not touch files.
    reported_keys = {r["key"] for r in result["reported"]}
    assert "token-optimizer@alexgreensh-token-optimizer" in reported_keys
    assert "alexgreensh-token-optimizer" in reported_keys
    assert result["removed"] == []
    assert inst.read_text(encoding="utf-8") == before_inst
    assert mkt.read_text(encoding="utf-8") == before_mkt


def test_remove_drops_only_our_stale_entries_leaving_others_byte_identical(tmp_path, monkeypatch):
    # FIX-SPEC-DAEMON: a genuinely stale identity (installPath gone AND daemon
    # dead) is still removed. Pin the liveness probe to False so the test is
    # deterministic regardless of whether a real daemon is running on the host.
    monkeypatch.setattr(ir, "_dashboard_daemon_alive", lambda: False)
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    cache = plugins_dir / "cache"
    our_install = cache / "alexgreensh-token-optimizer" / "token-optimizer" / "5.11.67"  # gone
    foreign_install = cache / "claude-plugins-official" / "ralph-loop" / "1.0.0"
    foreign_install.mkdir(parents=True)
    our_mkt_loc = plugins_dir / "marketplaces" / "alexgreensh-token-optimizer"  # gone
    foreign_mkt_loc = plugins_dir / "marketplaces" / "claude-plugins-official"
    foreign_mkt_loc.mkdir(parents=True)
    inst = _write_installed(plugins_dir, {
        "token-optimizer@alexgreensh-token-optimizer": [
            {"installPath": str(our_install), "version": "5.11.67"}
        ],
        "ralph-loop@claude-plugins-official": [
            {"installPath": str(foreign_install), "version": "1.0.0"}
        ],
    })
    mkt = _write_marketplaces(plugins_dir, {
        "alexgreensh-token-optimizer": {"installLocation": str(our_mkt_loc)},
        "claude-plugins-official": {"installLocation": str(foreign_mkt_loc)},
    })

    result = ir.reconcile_uninstall(home, dry_run=False, remove=True)
    removed = result["removed"]
    assert any("token-optimizer@alexgreensh-token-optimizer" in r for r in removed)
    assert any("alexgreensh-token-optimizer" in r for r in removed)
    # Backup written.
    assert result["backup"] is not None
    assert Path(result["backup"]).is_dir()
    # Foreign entries survive, byte-identical value.
    inst_data = json.loads(inst.read_text(encoding="utf-8"))
    assert "ralph-loop@claude-plugins-official" in inst_data["plugins"]
    assert "token-optimizer@alexgreensh-token-optimizer" not in inst_data["plugins"]
    mkt_data = json.loads(mkt.read_text(encoding="utf-8"))
    assert "claude-plugins-official" in mkt_data
    assert "alexgreensh-token-optimizer" not in mkt_data
    # Backup copies of the original files exist.
    assert (Path(result["backup"]) / "installed_plugins.json").is_file()
    assert (Path(result["backup"]) / "known_marketplaces.json").is_file()


def test_remove_keeps_active_our_entries(tmp_path):
    """A non-stale (still-installed) entry of ours is reported but NOT removed."""
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    cache = plugins_dir / "cache"
    our_install = cache / "alexgreensh-token-optimizer" / "token-optimizer" / "5.11.67"
    our_install.mkdir(parents=True)  # present -> active, not stale
    our_mkt_loc = plugins_dir / "marketplaces" / "alexgreensh-token-optimizer"
    our_mkt_loc.mkdir(parents=True)  # present
    inst = _write_installed(plugins_dir, {
        "token-optimizer@alexgreensh-token-optimizer": [
            {"installPath": str(our_install), "version": "5.11.67"}
        ],
    })
    mkt = _write_marketplaces(plugins_dir, {
        "alexgreensh-token-optimizer": {"installLocation": str(our_mkt_loc)},
    })
    before_inst = inst.read_text(encoding="utf-8")
    before_mkt = mkt.read_text(encoding="utf-8")

    result = ir.reconcile_uninstall(home, dry_run=False, remove=True)
    # Reported as not stale.
    our_reports = [r for r in result["reported"] if "token-optimizer" in r["key"]]
    assert all(not r["stale"] for r in our_reports)
    # Nothing removed (active entries stay).
    assert result["removed"] == []
    assert inst.read_text(encoding="utf-8") == before_inst
    assert mkt.read_text(encoding="utf-8") == before_mkt


def test_dry_run_with_remove_discloses_but_touches_nothing(tmp_path, monkeypatch):
    # FIX-SPEC-DAEMON: deterministic liveness (daemon dead) so the stale
    # classification holds regardless of a real daemon on the host.
    monkeypatch.setattr(ir, "_dashboard_daemon_alive", lambda: False)
    home = tmp_path / "home"
    plugins_dir = home / "plugins"
    cache = plugins_dir / "cache"
    our_install = cache / "alexgreensh-token-optimizer" / "token-optimizer" / "5.11.67"  # gone
    inst = _write_installed(plugins_dir, {
        "token-optimizer@alexgreensh-token-optimizer": [
            {"installPath": str(our_install), "version": "5.11.67"}
        ],
    })
    mkt = _write_marketplaces(plugins_dir, {
        "alexgreensh-token-optimizer": {"installLocation": str(plugins_dir / "marketplaces" / "x")},
    })
    before_inst = inst.read_text(encoding="utf-8")
    before_mkt = mkt.read_text(encoding="utf-8")

    result = ir.reconcile_uninstall(home, dry_run=True, remove=True)
    assert any("token-optimizer@" in r for r in result["removed"])  # would-remove
    # Nothing written; no backup dir created.
    assert result["backup"] is None
    assert inst.read_text(encoding="utf-8") == before_inst
    assert mkt.read_text(encoding="utf-8") == before_mkt


if __name__ == "__main__":
    import tempfile

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            with tempfile.TemporaryDirectory() as td:
                t(Path(td))
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
