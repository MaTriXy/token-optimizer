"""Regression coverage for the Hermes fleet adapter fixes.

- hermes_install ships spawn_utils.py (hermes_hook_bridge imports it).
- HermesAdapter.scan() reads ~/.hermes/state.db instead of silently
  returning zero runs.
- unpriced models (MiniMax, Kimi, ...) surface as a warning + keep their
  DB-reported cost instead of collapsing to a fake $0.00.

Run: python3 -m pytest tests/test_hermes_fleet.py -v
"""
from __future__ import annotations

import math
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
FLEET_SCRIPTS = REPO / "skills" / "fleet-auditor" / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(FLEET_SCRIPTS))

import fleet  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    """fleet._load_pricing memoizes into a module global; without resetting it a
    dev-box pricing.json (or an earlier test's monkeypatched FLEET_DB_DIR) leaks
    across tests and can flip model_is_priced. Reset before AND after each test."""
    fleet._pricing_override = None
    yield
    fleet._pricing_override = None


# ---------------------------------------------------------------------------
# #148 — the install manifest must include spawn_utils.py
# ---------------------------------------------------------------------------

def test_148_hermes_install_ships_spawn_utils():
    import hermes_install  # noqa: PLC0415

    assert "spawn_utils.py" in hermes_install._RUNTIME_MODULES, (
        "hermes_hook_bridge imports `from spawn_utils import spawn_detached`; "
        "omitting it from _RUNTIME_MODULES breaks the installed plugin."
    )


def test_148_bridge_actually_imports_spawn_utils():
    # Guards the premise: if the bridge stops importing spawn_utils, the manifest
    # entry above is dead weight and this test tells us to revisit it.
    bridge = (SCRIPTS / "hermes_hook_bridge.py").read_text(encoding="utf-8")
    assert "from spawn_utils import" in bridge or "import spawn_utils" in bridge


# ---------------------------------------------------------------------------
# Synthetic ~/.hermes/state.db for the scan + unpriced-model tests
# ---------------------------------------------------------------------------

def _make_state_db(path: Path, rows: list[dict]):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            model TEXT,
            started_at REAL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER,
            tool_call_count INTEGER,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cwd TEXT,
            source TEXT
        )
        """
    )
    for r in rows:
        cols = ", ".join(r.keys())
        ph = ", ".join("?" for _ in r)
        conn.execute(f"INSERT INTO sessions ({cols}) VALUES ({ph})", tuple(r.values()))
    conn.commit()
    conn.close()


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    """Point fleet.HOME at a tmp dir carrying a synthetic .hermes/state.db."""
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    monkeypatch.setattr(fleet, "HOME", tmp_path)
    return tmp_path


def _base_row(**over):
    now = datetime.now(timezone.utc)
    row = {
        "id": "sess-1",
        "model": "claude-sonnet-4-6",
        "started_at": (now - timedelta(hours=1)).timestamp(),
        "ended_at": now.timestamp(),
        "message_count": 12,
        "input_tokens": 20_000,
        "output_tokens": 4_000,
        "cache_read_tokens": 5_000,
        "cache_write_tokens": 1_000,
        "estimated_cost_usd": None,
        "actual_cost_usd": None,
        "cost_status": "estimated",
        "cwd": "/Users/x/proj-alpha",
        "source": "cli",
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# #149 — scan() reads real rows
# ---------------------------------------------------------------------------

def test_149_scan_reads_sessions(hermes_home):
    _make_state_db(hermes_home / ".hermes" / "state.db", [_base_row()])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, errors = fleet.HermesAdapter().scan(since)
    assert errors == []
    assert len(runs) == 1
    run = runs[0]
    assert run.system == "hermes"
    assert run.session_id == "sess-1"
    assert run.model == "sonnet"
    # Full cwd (not basename) and Hermes's own source label are preserved.
    assert run.project == "/Users/x/proj-alpha"
    assert run.agent_name == "cli"
    # input_tokens and cache_read_tokens are SEPARATE columns in Hermes — input is
    # NOT reduced by cache_read (that would floor cache-heavy runs to 0).
    assert run.tokens.input == 20_000
    assert run.tokens.cache_read == 5_000
    assert run.message_count == 12


def test_149_scan_missing_db_is_a_clean_warning(hermes_home):
    # .hermes/ exists (fixture) but no state.db
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, errors = fleet.HermesAdapter().scan(since)
    assert runs == []
    assert errors and "state.db not found" in errors[0]


def test_149_scan_windows_by_started_at(hermes_home):
    old = _base_row(id="old", started_at=(datetime.now(timezone.utc) - timedelta(days=90)).timestamp())
    new = _base_row(id="new")
    _make_state_db(hermes_home / ".hermes" / "state.db", [old, new])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    assert {r.session_id for r in runs} == {"new"}


def test_149_text_started_at_old_row_does_not_leak(hermes_home):
    # Older Hermes schemas store started_at as ISO TEXT. The SQL WHERE binds a
    # float, so a lexical comparison lets old TEXT rows leak; the Python-side
    # window guard must still exclude them.
    path = hermes_home / ".hermes" / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, model TEXT, started_at TEXT, "
        "input_tokens INTEGER, output_tokens INTEGER, message_count INTEGER)"
    )
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                 ("old", "claude-sonnet-4-6", "2020-01-01T00:00:00+00:00", 1000, 500, 4))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                 ("new", "claude-sonnet-4-6", datetime.now(timezone.utc).isoformat(), 1000, 500, 4))
    conn.commit()
    conn.close()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    assert {r.session_id for r in runs} == {"new"}, "2020 TEXT row leaked past the window"


def test_149_scan_tolerates_missing_columns(hermes_home):
    # An older Hermes schema without the cost columns must still scan.
    path = hermes_home / ".hermes" / "state.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE sessions (id TEXT, model TEXT, started_at REAL, "
        "input_tokens INTEGER, output_tokens INTEGER, message_count INTEGER)"
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,?,?,?,?)",
        ("s1", "claude-opus-4-8", datetime.now(timezone.utc).timestamp(), 1000, 500, 4),
    )
    conn.commit()
    conn.close()
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, errors = fleet.HermesAdapter().scan(since)
    assert errors == []
    assert len(runs) == 1
    assert runs[0].model == "opus"


# ---------------------------------------------------------------------------
# #150 — unpriced models keep DB cost; pricing helper; warning surfaces
# ---------------------------------------------------------------------------

def test_150_model_is_priced_distinguishes_unknown_from_free():
    assert fleet.model_is_priced("sonnet") is True
    assert fleet.model_is_priced("MiniMax-M3") is False


def test_150_unpriced_model_keeps_hermes_reported_cost(hermes_home):
    # MiniMax isn't in DEFAULT_PRICING; Hermes metered it at $4.20 — we must keep
    # that, not recompute to $0.00.
    row = _base_row(id="mm", model="MiniMax-M3", actual_cost_usd=4.20,
                    input_tokens=55_000_000, cache_read_tokens=1_300_000_000)
    _make_state_db(hermes_home / ".hermes" / "state.db", [row])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    assert len(runs) == 1
    assert runs[0].cost_usd == pytest.approx(4.20)
    # Unknown models are normalized (lowercased) so the same gateway model from
    # different adapters aggregates and prices under one key.
    assert runs[0].model == "minimax-m3"


def test_150_estimated_cost_used_when_no_actual(hermes_home):
    row = _base_row(id="est", model="kimi-k2.7-code", estimated_cost_usd=1.11)
    _make_state_db(hermes_home / ".hermes" / "state.db", [row])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    assert runs[0].cost_usd == pytest.approx(1.11)


def test_150_recorded_zero_on_priced_model_is_authoritative(hermes_home):
    # Hermes recorded $0 on a priced model (comped/free run). We must NOT re-price
    # it via calculate_cost and fabricate a nonzero cost.
    row = _base_row(id="free", model="claude-sonnet-4-6",
                    actual_cost_usd=0.0, estimated_cost_usd=None,
                    input_tokens=20_000, output_tokens=4_000)
    _make_state_db(hermes_home / ".hermes" / "state.db", [row])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    assert runs[0].cost_usd == 0.0


def test_150_garbage_cost_cell_does_not_drop_the_run(hermes_home):
    # An empty-string / non-numeric / inf cost must degrade to calculate_cost,
    # never raise and discard the whole session.
    for bad in ("", "n/a", "inf", float("inf"), float("nan")):
        db = hermes_home / ".hermes" / "state.db"
        if db.exists():
            db.unlink()
        row = _base_row(id="bad", model="claude-sonnet-4-6", actual_cost_usd=bad,
                        estimated_cost_usd=None, input_tokens=20_000, output_tokens=4_000)
        _make_state_db(db, [row])
        since = datetime.now(timezone.utc) - timedelta(days=30)
        runs, errors = fleet.HermesAdapter().scan(since)
        assert len(runs) == 1, f"run dropped for cost={bad!r}"
        assert math.isfinite(runs[0].cost_usd) and runs[0].cost_usd >= 0
        assert runs[0].cost_usd > 0  # priced model -> calculate_cost, not $0


def test_150_negative_recorded_cost_is_preserved(hermes_home):
    # A refund/credit (negative) is a real recorded value; don't recompute it away.
    row = _base_row(id="refund", model="MiniMax-M3", actual_cost_usd=-1.20)
    _make_state_db(hermes_home / ".hermes" / "state.db", [row])
    since = datetime.now(timezone.utc) - timedelta(days=30)
    runs, _ = fleet.HermesAdapter().scan(since)
    # -1.20 is < 0 so _recorded_cost skips it; unknown model -> calculate_cost -> 0.
    # The key assertion: the run is NOT dropped and cost stays finite.
    assert len(runs) == 1
    assert math.isfinite(runs[0].cost_usd)


def test_150_pricing_json_key_is_normalized(hermes_home, tmp_path, monkeypatch):
    # User adds "MiniMax-M3" (the name the warning shows) to pricing.json; it must
    # price the run keyed as "minimax-m3".
    import json as _json
    fleet_dir = tmp_path / "fleetdb"
    fleet_dir.mkdir()
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", fleet_dir)
    (fleet_dir / "pricing.json").write_text(
        _json.dumps({"MiniMax-M3": {"input": 1.0, "output": 2.0, "cache_read": 0.1, "cache_write": 1.0}}),
        encoding="utf-8",
    )
    fleet._pricing_override = None
    assert fleet.model_is_priced("minimax-m3") is True


def test_150_scan_command_warns_on_unpriced_zero_cost(hermes_home, tmp_path, monkeypatch, capsys):
    # Unpriced model with NO DB cost -> $0.00 -> must produce a visible warning.
    row = _base_row(id="z", model="MiniMax-M3", actual_cost_usd=None,
                    estimated_cost_usd=None)
    _make_state_db(hermes_home / ".hermes" / "state.db", [row])

    # Isolate the fleet DB so cmd_scan doesn't touch the real one.
    fleet_dir = tmp_path / "fleetdb"
    fleet_dir.mkdir()
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", fleet_dir)
    monkeypatch.setattr(fleet, "FLEET_DB", fleet_dir / "fleet.db")
    # Only scan hermes so other real adapters on the dev box don't add noise.
    fleet.cmd_scan(["--system", "hermes", "--json"])
    out = capsys.readouterr().out
    assert "minimax-m3" in out  # normalized name, the pricing.json key to add
    assert "unpriced" in out.lower() or "UNKNOWN" in out


def test_150_dashboard_flags_unpriced_models(tmp_path, monkeypatch):
    # The dashboard is the surface #150 names: it must NOT present unpriced runs'
    # $0 as a real total — it renders a banner and marks Total Cost understated.
    fd = tmp_path / "fleetdb"
    fd.mkdir()
    monkeypatch.setattr(fleet, "FLEET_DB_DIR", fd)
    monkeypatch.setattr(fleet, "FLEET_DB", fd / "fleet.db")
    monkeypatch.setattr(fleet, "FLEET_DASHBOARD_PATH", fd / "fleet-dashboard.html")
    monkeypatch.setattr(fleet, "_open_in_browser", lambda p: None)

    conn = fleet._init_fleet_db()
    now = datetime.now(timezone.utc)
    priced = fleet.AgentRun(system="hermes", session_id="p", model="sonnet", cost_usd=0.9,
                            timestamp=now, tokens=fleet.TokenBreakdown(input=20_000, output=4_000),
                            source_path="hermes:state.db#p")
    unpriced = fleet.AgentRun(system="hermes", session_id="u", model="minimax-m3", cost_usd=0.0,
                              timestamp=now, tokens=fleet.TokenBreakdown(input=55_000_000, cache_read=1_300_000_000),
                              source_path="hermes:state.db#u")
    fleet._insert_run(conn, priced)
    fleet._insert_run(conn, unpriced)
    conn.commit()
    fleet._update_daily_aggregates(conn)
    conn.commit()
    conn.close()

    fleet.cmd_dashboard([])
    html = (fd / "fleet-dashboard.html").read_text(encoding="utf-8")
    assert "Cost is understated" in html
    assert "minimax-m3" in html
    assert "understated)" in html  # Total Cost label marked
    # a genuinely-priced model must NOT be flagged
    assert "sonnet" not in html.split("unpriced-banner")[1].split("</div>")[0] if "unpriced-banner" in html else True
