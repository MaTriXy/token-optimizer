#!/usr/bin/env python3
"""Doc-assertion test for docs/uninstall.md.

Locks the documented uninstall contract to the implemented behavior so a
future refactor can't silently drift the docs from the code. Asserts:
- The ``cleanup`` command is documented and exists in measure.py.
- The ``--this-install-only`` flag is documented and wired.
- The ``--dry-run`` retained-paths disclosure is documented.
- The self-disabling status line guard is documented.
- The identity-sweeping behavior is documented.
- The preserved-by-design paths (session data, checkpoints, trends) are
  documented.

Run directly:  python3 tests/test_uninstall_docs_assertions.py
Exits non-zero on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "uninstall.md"
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def test_cleanup_command_documented_and_implemented():
    text = DOC.read_text(encoding="utf-8")
    assert "measure.py cleanup" in text, "cleanup command not documented"
    assert "cleanup --dry-run" in text, "cleanup --dry-run not documented"
    import measure
    assert hasattr(measure, "cleanup"), "measure.cleanup not implemented"
    assert callable(measure.cleanup), "measure.cleanup is not callable"


def test_this_install_only_documented_and_wired():
    text = DOC.read_text(encoding="utf-8")
    assert "--this-install-only" in text, "--this-install-only not documented"
    import measure
    # setup_daemon accepts this_install_only
    import inspect
    sig = inspect.signature(measure.setup_daemon)
    assert "this_install_only" in sig.parameters, "setup_daemon missing this_install_only param"


def test_dry_run_retained_paths_disclosure_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "retained-paths disclosure" in text.lower() or "retained-paths" in text.lower(), \
        "retained-paths disclosure not documented"
    assert "Retained by design" in text or "preserved by design" in text.lower(), \
        "preserved-by-design section not documented"


def test_self_disabling_statusline_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "self-disabling" in text.lower() or "self-disables" in text.lower(), \
        "self-disabling status line guard not documented"


def test_identity_sweeping_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "token-optimizer-*" in text, "identity-sweeping (token-optimizer-*) not documented"
    assert "sweep" in text.lower() or "all token-optimizer" in text.lower(), \
        "sweep-all behavior not documented"


def test_preserved_paths_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "session data" in text.lower(), "session data preservation not documented"
    assert "checkpoints" in text.lower(), "checkpoints preservation not documented"
    assert "trends" in text.lower(), "trends preservation not documented"


def test_manifest_reconcile_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "installed_plugins.json" in text, "installed_plugins.json reconcile not documented"
    assert "known_marketplaces.json" in text, "known_marketplaces.json reconcile not documented"
    import install_reconcile
    assert hasattr(install_reconcile, "reconcile_uninstall"), \
        "install_reconcile.reconcile_uninstall not implemented"


def test_backup_before_modify_documented():
    text = DOC.read_text(encoding="utf-8")
    assert "back up" in text.lower() or "backup" in text.lower(), \
        "backup-before-modify contract not documented"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
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
