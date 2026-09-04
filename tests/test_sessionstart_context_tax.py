"""Unit A: kill the SessionStart context tax.

SessionStart hooks inject their stdout into the agent's context window every
session. Diagnostic lines printed to stdout land in the model context on every
turn, costing tokens with no benefit to the model.

These tests pin the fix:
  * the systemd ``[Error] systemctl ... is not reachable`` block routes to
    stderr under ``soft_fail`` (hook path) and stays out of stdout, which is
    well under the 150-char SessionStart budget;
  * the macOS launchd installer's ``_fail`` does the same (same bug class);
  * the "Self-healed N hooks" notice routes to stderr so it never inflates
    SessionStart context, regardless of invocation mode.
"""

from __future__ import annotations

import contextlib
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
MEASURE_PY = SCRIPTS / "measure.py"

sys.path.insert(0, str(SCRIPTS))


@pytest.fixture()
def m(monkeypatch):
    """measure.py imported with its state dir pinned into a temp dir."""
    tmp = tempfile.mkdtemp(prefix="to-sstax-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    monkeypatch.setattr(mod, "_is_foreign_runtime", lambda: False)
    monkeypatch.setattr(mod, "detect_runtime", lambda: "claude")
    _legacy_sandbox = Path(tmp) / "legacy"
    monkeypatch.setattr(mod, "_legacy_daemon_dir",
                        lambda: _legacy_sandbox, raising=False)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _flags_env(m, monkeypatch, initial=None):
    """Dict-backed config flags so throttle stamps never touch the REAL
    config.json (CONFIG_PATH is not sandboxed by SNAPSHOT_DIR)."""
    flags = dict(initial or {})
    monkeypatch.setattr(m, "_read_config_flag",
                        lambda k, d=None: flags.get(k, d if d is not None else False))
    monkeypatch.setattr(m, "_write_config_flag",
                        lambda k, v: flags.__setitem__(k, v))
    return flags


# ---------------------------------------------------------------------------
# The 594-char SessionStart context tax: systemd "is not reachable" diagnostic
# ---------------------------------------------------------------------------
def test_systemd_unreachable_error_stays_out_of_hook_stdout(m, monkeypatch, capsys):
    """On a headless/container/CI Linux box without a reachable systemd
    user bus, the installer must NOT print the ``[Error] systemctl ... is not
    reachable`` block to stdout (which is SessionStart context). Under
    soft_fail (the hook path) it routes to stderr; stdout stays clean and
    well under the 150-char SessionStart budget."""
    # Simulate a dead/absent systemd user bus (the headless/container case).
    monkeypatch.setattr(m, "_probe_systemd_user_bus", lambda: False)

    result = m._install_systemd_user_daemon(soft_fail=True)

    assert result is False, "soft_fail installer must return False, not raise"
    out, err = capsys.readouterr()
    assert "systemctl" not in out, (
        "systemctl diagnostic leaked into hook stdout (SessionStart context)"
    )
    assert "is not reachable" not in out, (
        "'is not reachable' diagnostic leaked into hook stdout"
    )
    assert len(out) < 150, (
        f"headless SessionStart stdout is {len(out)} chars, must be < 150; "
        f"got: {out!r}"
    )
    # The diagnostic must still be visible to a human reading stderr / logs.
    assert "is not reachable" in err, (
        "the systemctl diagnostic must still reach stderr for diagnostics"
    )


def test_systemd_error_still_prints_to_stdout_for_interactive_cli(m, monkeypatch, capsys):
    """A human running ``measure.py setup-daemon`` interactively
    (soft_fail=False) must still see the actionable error on stdout -- we only
    suppress the injected-context path, never genuine interactive diagnostics."""
    monkeypatch.setattr(m, "_probe_systemd_user_bus", lambda: False)

    with pytest.raises(SystemExit):
        m._install_systemd_user_daemon(soft_fail=False)

    out, err = capsys.readouterr()
    assert "is not reachable" in out, (
        "interactive CLI run must keep the actionable error on stdout"
    )


# ---------------------------------------------------------------------------
# macOS launchd: same bug class, same fix
# ---------------------------------------------------------------------------
def test_launchd_error_stays_out_of_hook_stdout(m, monkeypatch, capsys):
    """The macOS launchd installer's _fail must route to stderr under
    soft_fail too, so a headless macOS run never injects a daemon error into
    SessionStart context."""
    monkeypatch.setattr(m, "_ensure_dashboard_file", lambda **kw: False)

    result = m._install_launchd_daemon(soft_fail=True)

    assert result is False
    out, err = capsys.readouterr()
    assert "Dashboard file missing" not in out, (
        "launchd error leaked into hook stdout"
    )
    assert len(out) < 150, (
        f"headless stdout is {len(out)} chars, must be < 150; got: {out!r}"
    )
    assert "Dashboard file missing" in err


# ---------------------------------------------------------------------------
# "Self-healed N hooks" notice: route to stderr on the hook path
# ---------------------------------------------------------------------------
def _stub_ensure_health_to_heal(m, monkeypatch, added):
    """Stub the I/O-heavy ensure-health surface so run_ensure_health reaches
    the hook-heal block (the 'Self-healed N hooks' print) without touching the
    real filesystem or emitting unrelated stdout."""
    flags = _flags_env(m, monkeypatch, {
        "v5_welcome_shown": True,          # skip first-run welcome
        "enterprise_consent_shown": True,
        "last_hook_heal_check": 0,         # stale -> heal block runs
    })
    # Early ensure-health I/O that could print to stdout.
    monkeypatch.setattr(m, "_read_settings_json",
                        lambda: ({"cleanupPeriodDays": 99999}, None))
    monkeypatch.setattr(m, "_auto_remove_bad_env_vars", lambda: None)
    monkeypatch.setattr(m, "_auto_capture_pristine_baseline", lambda: False)
    # Dashboard staleness block: skip by pretending no dashboard file exists.
    monkeypatch.setattr(m, "DASHBOARD_PATH", Path(m.TOKEN_OPTIMIZER_SNAPSHOT_DIR
                                                  if hasattr(m, "TOKEN_OPTIMIZER_SNAPSHOT_DIR")
                                                  else tempfile.gettempdir()) / "nope.html")
    # Daemon self-heal: return a quiet no-op state (no stdout print).
    monkeypatch.setattr(m, "_ensure_dashboard_daemon", lambda *a, **k: "noop-healthy")
    monkeypatch.setattr(m, "maybe_sweep_stale_leases", lambda: None)
    monkeypatch.setattr(m, "_ensure_vscode_extension", lambda: None)
    monkeypatch.setattr(m, "keepwarm_scheduler_repair", lambda: None)
    # Windows-only welcome: force non-Windows.
    monkeypatch.setattr(m.platform, "system", lambda: "Darwin")
    # The heal block itself.
    monkeypatch.setattr(m, "_reconcile_sessionend_fossils",
                        lambda: {"rewritten": 0, "removed": 0, "stop_removed": 0})
    monkeypatch.setattr(m, "_is_plugin_installed", lambda: False)
    monkeypatch.setattr(m, "setup_all_hooks",
                        lambda dry_run=False, verbose=False: {"added": added})
    # Later one-time/repair notices that could print to stdout.
    monkeypatch.setattr(m, "_star_session_pitch", lambda: "")
    monkeypatch.setattr(m, "_fix_stale_settings_paths", lambda: 0)
    monkeypatch.setattr(m, "_migrate_statusline_to_stable_path", lambda: False)
    monkeypatch.setattr(m, "_heal_keepwarm_plist_path", lambda: False)
    monkeypatch.setattr(m, "_heal_windows_console_flash", lambda: False)
    monkeypatch.setattr(m, "_fix_malformed_hook_commands", lambda: 0)
    return flags


def test_self_healed_notice_routes_to_stderr_under_hook(m, monkeypatch, capsys):
    """When ensure-health runs as a non-interactive hook and heals missing
    hooks, the 'Self-healed N hooks' notice must go to stderr (not stdout),
    so it never inflates SessionStart context. An interactive run keeps it on
    stdout.

    Two sub-paths exist under the hook:
      - HEAL path: settings.json has some of our hooks (genuine drift) →
        setup_all_hooks runs, 'Self-healed N' goes to stderr.
      - SKIP path: settings.json has ZERO of our hooks (hooks from another
        settings layer) → heal is skipped to avoid double-registration, and a
        short skip reason goes to stderr instead.

    Both paths must write their diagnostic to stderr. Neither may write the
    diagnostic to stdout (SessionStart context). A silent skip is a failure
    mode, not a fix: when we decline to heal we say we declined and why."""
    # --- HEAL path: settings.json has our hooks, heal runs ---
    _stub_ensure_health_to_heal(m, monkeypatch, added=1)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)
    monkeypatch.setattr(m, "_should_skip_self_heal_hooks", lambda: (False, True))

    m.run_ensure_health()

    out, err = capsys.readouterr()
    assert "Self-healed" not in out, (
        "Self-healed notice leaked into hook stdout (SessionStart context)"
    )
    assert "Self-healed" in err, (
        "Self-healed notice must still reach stderr for diagnostics"
    )
    assert len(out) < 150, (
        f"headless stdout is {len(out)} chars, must be < 150; got: {out!r}"
    )

    # --- SKIP path: settings.json has zero of our hooks, heal is skipped ---
    _stub_ensure_health_to_heal(m, monkeypatch, added=1)
    monkeypatch.setattr(m, "_running_under_hook", lambda: True)
    monkeypatch.setattr(m, "_should_skip_self_heal_hooks", lambda: (True, True))

    m.run_ensure_health()

    out, err = capsys.readouterr()
    assert "Self-healed" not in out, (
        "heal was skipped but 'Self-healed' leaked into stdout"
    )
    assert "skipping heal" in err, (
        "skip path must emit a stderr diagnostic explaining why heal was skipped; "
        "a silent skip is the failure mode, not the fix"
    )
    assert len(out) < 150, (
        f"headless stdout is {len(out)} chars, must be < 150; got: {out!r}"
    )


def test_self_healed_notice_stays_on_stdout_for_interactive_run(m, monkeypatch, capsys):
    """An interactive ``measure.py ensure-health`` (a tty, not a hook) must
    still surface the Self-healed notice so a human sees it. The notice now
    routes to stderr (visible on the terminal), keeping stdout clean for both
    hook and interactive invocations."""
    _stub_ensure_health_to_heal(m, monkeypatch, added=1)
    monkeypatch.setattr(m, "_running_under_hook", lambda: False)

    m.run_ensure_health()

    _, err = capsys.readouterr()
    assert "Self-healed" in err, (
        "interactive run must still surface the Self-healed notice on stderr"
    )
