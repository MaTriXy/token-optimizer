#!/usr/bin/env python3
"""Self-disabling guard in statusline.js.

Claude Code has no plugin uninstall/teardown hook, so a ``/plugin uninstall``
or a manual ``rm -rf`` of the plugin tree can leave the ``statusLine`` command
in ``settings.json`` pointing at a script whose plugin tree has been removed.
A missing command renders the status line silently blank (the failure is only
visible under ``claude --debug``), with no clue that Token Optimizer was the
cause.

The fix is a self-disabling guard at the top of ``statusline.js``: if the
plugin tree that ships the script has been gutted (the canonical sibling
``measure.py`` is gone while ``statusline.js`` remains), the script exits 0
with no output and no stderr spew. A blank line is what a missing command
already produces, so this changes nothing while installed and removes the
dangling-reference state after a partial removal.

The command string shape is intentionally NOT changed for existing installs
(locked design decision), so the matcher at ``measure.py`` that recognizes
our statusLine (``"statusline.js"`` + ``"token-optimizer"``) keeps working
for both old and any new shape.

Run directly:  python3 tests/test_statusline_self_disable.py
Exits non-zero on first failure.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
STATUSLINE = SCRIPTS / "statusline.js"
sys.path.insert(0, str(SCRIPTS))


def _node_available() -> bool:
    return shutil.which("node") is not None


def _run_statusline(script_path: Path, stdin_payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["node", str(script_path)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        timeout=10,
    )


def _minimal_payload() -> dict:
    return {
        "model": {"display_name": "Test"},
        "workspace": {"current_dir": str(REPO)},
        "session_id": "test-session-1",
    }


def test_guard_exits_quietly_when_plugin_tree_gutted(tmp_path):
    """statusline.js with its sibling measure.py removed exits 0, empty, no stderr."""
    if not _node_available():
        print("  SKIP  node not available")
        return
    # Copy the script tree into a temp install so we can gut it without
    # touching the real repo. A real plugin-cache removal deletes the whole
    # version dir; we simulate the partial-removal case the guard targets
    # (statusline.js present, sibling measure.py gone).
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    # Do NOT copy measure.py -> plugin tree gutted.
    result = _run_statusline(install / "statusline.js", _minimal_payload())
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}; stderr={result.stderr!r}"
    assert result.stdout == "", f"expected empty stdout, got {result.stdout!r}"
    assert result.stderr == "", f"expected empty stderr, got {result.stderr!r}"


def test_guard_does_not_fire_when_tree_intact(tmp_path):
    """With measure.py present, statusline.js renders normally (no self-disable)."""
    if not _node_available():
        print("  SKIP  node not available")
        return
    install = tmp_path / "scripts"
    install.mkdir()
    shutil.copy2(STATUSLINE, install / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", install / "measure.py")
    result = _run_statusline(install / "statusline.js", _minimal_payload())
    assert result.returncode == 0, f"expected exit 0, got {result.returncode}; stderr={result.stderr!r}"
    # A working status line emits the model name from the payload.
    assert "Test" in result.stdout, (
        f"guard fired on an intact tree (stdout empty); stdout={result.stdout!r}"
    )


def _build_clone_install(tmp_path, mkt="alexgreensh-token-optimizer", *, with_cache=True, version="5.11.67"):
    """Marketplace-clone layout the statusLine points at for plugin-cache installs.

    Returns the clone's statusline.js path. When ``with_cache`` the plugin's
    cache tree exists (healthy install); otherwise it is absent (the state after
    a native ``/plugin uninstall``, which removes the cache tree but keeps the
    shared marketplace clone).
    """
    plugins = tmp_path / "claude" / "plugins"
    clone = plugins / "marketplaces" / mkt / "skills" / "token-optimizer" / "scripts"
    clone.mkdir(parents=True)
    shutil.copy2(STATUSLINE, clone / "statusline.js")
    shutil.copy2(SCRIPTS / "measure.py", clone / "measure.py")  # clone keeps its sibling
    if with_cache:
        (plugins / "cache" / mkt / "token-optimizer" / version).mkdir(parents=True)
    return clone / "statusline.js"


def test_clone_statusline_self_disables_after_plugin_uninstall(tmp_path):
    """G3 C-P2-2: statusLine runs from the surviving marketplace clone, but the
    plugin's cache tree is gone -> the plugin is uninstalled -> render nothing."""
    if not _node_available():
        print("  SKIP  node not available")
        return
    sl = _build_clone_install(tmp_path, with_cache=False)
    result = _run_statusline(sl, _minimal_payload())
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == "", f"ghost status line rendered after uninstall: {result.stdout!r}"
    assert result.stderr == ""


def test_clone_statusline_renders_while_plugin_still_installed(tmp_path):
    """Healthy install: clone present AND cache tree present -> normal render."""
    if not _node_available():
        print("  SKIP  node not available")
        return
    sl = _build_clone_install(tmp_path, with_cache=True)
    result = _run_statusline(sl, _minimal_payload())
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "Test" in result.stdout, (
        f"guard fired on a healthy clone install (stdout empty): {result.stdout!r}"
    )


def test_uninstall_matcher_still_recognizes_command_shape():
    """The settings.json statusLine uninstall predicate must still match.

    The locked decision is to NOT change the command string shape, so the
    existing matcher (``"statusline.js" in cmd and "token-optimizer" in cmd``)
    must keep recognizing the installed command. This guards against a future
    refactor that silently breaks uninstall for already-installed users.
    """
    import measure

    def _is_ours(cmd: str) -> bool:
        return "statusline.js" in cmd and "token-optimizer" in cmd

    # The exact shape setup-quality-bar writes today.
    real_cmd = f"node '{SCRIPTS / 'statusline.js'}'"
    assert _is_ours(real_cmd), f"matcher rejected the real command: {real_cmd}"
    # The install predicate in measure.py must agree.
    sl = {"command": real_cmd}
    detected = measure._is_quality_bar_installed({"statusLine": sl})
    assert detected["statusline"] is True, "measure.py no longer detects our statusLine"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            # tests with a tmp_path fixture need pytest; fall back to a temp dir
            import tempfile

            if "tmp_path" in t.__code__.co_varnames:
                with tempfile.TemporaryDirectory() as td:
                    t(Path(td))
            else:
                t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
