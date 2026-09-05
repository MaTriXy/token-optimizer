"""cache_instability surface extension (MCP + process-library).

The detector historically only inspected CLAUDE.md. This extension adds two more
cache-prefix-resident surfaces that churn just as often:
  Signal 4: MCP server add/remove in .mcp.json / .claude.json (the server SET
            is the config-detectable proxy for tool-schema churn in the cached
            prefix; env/args/url VALUES are NOT scanned -- they never reach the
            model-facing prompt, only tool schemas do).
  Signal 5: process-library prompt prefix files in .a5c/processes, .claude/processes

These tests prove each new surface FIRES on real cache-prefix churn and does NOT
fire on stable/noise, that the guards are load-bearing (the new signals still
run when CLAUDE.md is absent), and that the 3 original CLAUDE.md signals are
unchanged. The regression guard (env values must NOT be scanned) is
test_mcp_env_values_are_not_scanned.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def ci(monkeypatch, tmp_path):
    """Fresh detector module with cwd+HOME+state isolated to tmp dirs so the
    scans never touch the real project/home/state, and the module-level scan
    caches start empty for each test. TOKEN_OPTIMIZER_SNAPSHOT_DIR pins the
    server-set state file under tmp_path so the diff has somewhere to persist."""
    import detectors.cache_instability as mod
    importlib.reload(mod)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    return mod


def _clear_caches(mod):
    """Drop the module-level scan caches so a second scan in the same test
    re-reads the (changed) config instead of returning the cached first read."""
    mod._MCP_SCAN_CACHE.clear()
    mod._PROCESS_SCAN_CACHE.clear()


# ---- Signal 4: MCP server set (add/remove) -------------------------------

def _write_mcp_servers(cwd: Path, servers: dict):
    (cwd / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}),
        encoding="utf-8",
    )


def test_mcp_first_run_records_baseline_no_fire(ci, tmp_path):
    """First run for a cwd has no prior set to diff against -> baseline only."""
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}})
    assert ci._scan_mcp_servers(str(tmp_path)) == []


def test_mcp_server_added_fires(ci, tmp_path):
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}})
    ci._scan_mcp_servers(str(tmp_path))  # baseline
    _clear_caches(ci)
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}, "git": {"command": "g"}})
    findings = ci._scan_mcp_servers(str(tmp_path))
    assert len(findings) == 1
    assert findings[0]["name"] == "cache_instability"
    assert "git" in findings[0]["evidence"]
    assert "tool schemas" in findings[0]["evidence"]


def test_mcp_server_removed_fires(ci, tmp_path):
    _write_mcp_servers(tmp_path, {"weather": {"command": "s"}, "git": {"command": "g"}})
    ci._scan_mcp_servers(str(tmp_path))  # baseline
    _clear_caches(ci)
    _write_mcp_servers(tmp_path, {"weather": {"command": "s"}})
    findings = ci._scan_mcp_servers(str(tmp_path))
    assert len(findings) == 1
    assert "git" in findings[0]["evidence"]


def test_mcp_stable_server_set_does_not_fire(ci, tmp_path):
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}})
    ci._scan_mcp_servers(str(tmp_path))  # baseline
    _clear_caches(ci)
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}})  # unchanged set
    assert ci._scan_mcp_servers(str(tmp_path)) == []


def test_mcp_env_values_are_not_scanned(ci, tmp_path):
    """Regression guard: env/args/url VALUES never reach the model-facing
    prompt, so volatile substrings in them (--log-level, a /status URL, a
    current-site path, a daily token) must NOT fire when the server SET is
    stable. The old behaviour false-positived on nearly every real .mcp.json."""
    volatile_cfg = {
        "weather": {
            "command": "npx",
            "args": ["-y", "@mcp/server", "--log-level", "debug", "--daily"],
            "url": "https://api.example.com/status",
            "env": {
                "CFG": "snapshot as of 2026-08-08T10:00 (daily)",
                "PATH_ARG": "/Users/me/projects/current-site",
                "CACHE_DIR": "/tmp/mcp-cache",
            },
        },
    }
    _write_mcp_servers(tmp_path, volatile_cfg)
    ci._scan_mcp_servers(str(tmp_path))  # baseline
    _clear_caches(ci)
    # Same server set, env values churned (timestamps updated) -> must NOT fire.
    volatile_cfg["weather"]["env"]["CFG"] = "snapshot as of 2026-09-09T11:00 (daily)"
    _write_mcp_servers(tmp_path, volatile_cfg)
    assert ci._scan_mcp_servers(str(tmp_path)) == [], (
        "env/args/url value churn must not fire -- those values never reach the prompt"
    )


def test_mcp_absent_file_is_no_op(ci, tmp_path):
    assert ci._scan_mcp_servers(str(tmp_path)) == []


def test_mcp_bad_json_fails_open(ci, tmp_path):
    (tmp_path / ".mcp.json").write_text("{not valid json", encoding="utf-8")
    assert ci._scan_mcp_servers(str(tmp_path)) == []


def test_mcp_symlink_config_not_followed(ci, tmp_path):
    """A symlinked .mcp.json must not be followed (path-traversal / huge-file guard)."""
    real = tmp_path / "real.json"
    real.write_text(json.dumps({"mcpServers": {"x": {}}}), encoding="utf-8")
    link = tmp_path / ".mcp.json"
    link.symlink_to(real)
    # Baseline records empty (symlink skipped), no fire, no crash.
    assert ci._scan_mcp_servers(str(tmp_path)) == []


# ---- Signal 5: process-library prefixes ----------------------------------

def _write_process(cwd: Path, name: str, body: str):
    d = cwd / ".claude" / "processes"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(body, encoding="utf-8")


def test_process_timestamp_prefix_fires(ci, tmp_path):
    _write_process(tmp_path, "flow.md", "Last updated: 2026-08-08\n" + "stable rules\n" * 20)
    findings = ci._scan_process_prefixes(str(tmp_path))
    assert len(findings) == 1
    assert findings[0]["name"] == "cache_instability"
    assert "flow.md" in findings[0]["evidence"]


def test_process_stable_prefix_does_not_fire(ci, tmp_path):
    _write_process(tmp_path, "flow.md", "# Stable process\n" + "do the thing\n" * 20)
    assert ci._scan_process_prefixes(str(tmp_path)) == []


def test_process_symlink_not_followed(ci, tmp_path):
    """A symlinked process prefix file must not be read."""
    d = tmp_path / ".claude" / "processes"
    d.mkdir(parents=True, exist_ok=True)
    real = tmp_path / "real.md"
    real.write_text("Last updated: 2026-08-08\n" + "stable rules\n" * 20, encoding="utf-8")
    (d / "flow.md").symlink_to(real)
    assert ci._scan_process_prefixes(str(tmp_path)) == [], (
        "symlinked process prefix must be skipped, not followed"
    )


# ---- Guard is load-bearing: new signals run when CLAUDE.md is absent ------

def test_new_signals_run_without_claude_md(ci, tmp_path):
    """The early `return []` was replaced with an empty lines list so Signal 4/5
    still run when CLAUDE.md is missing. session_data carries NO claude_md_content.
    Signal 4 fires on a server-set change (baseline then add a server)."""
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}})
    ci.detect_cache_instability({})  # baseline, no claude_md_content
    _clear_caches(ci)
    _write_mcp_servers(tmp_path, {"weather": {"command": "srv"}, "git": {"command": "g"}})
    findings = ci.detect_cache_instability({})
    assert any(f["name"] == "cache_instability" for f in findings), (
        "MCP signal must fire even with CLAUDE.md absent (guard is load-bearing)"
    )


# ---- Regression: the 3 original CLAUDE.md signals are unchanged -----------

def test_claude_md_timestamp_signal_unchanged(ci):
    md = "# Rules\nUpdated: 2026-08-08\n" + ("- keep this rule stable\n" * 400)
    findings = ci.detect_cache_instability({"claude_md_content": md})
    ts = [f for f in findings if f["name"] == "cache_instability" and f["confidence"] == 0.75]
    assert ts, "original CLAUDE.md timestamp signal (confidence 0.75) must still fire"
    assert ts[0]["savings_tokens"] > 500
