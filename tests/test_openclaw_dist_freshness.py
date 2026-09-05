#!/usr/bin/env python3
"""Dist-freshness guard for the OpenClaw shipped artifact (GAUNTLET C1).

The OpenClaw extension entry (``dist/index.js``) ``require``s the committed
``dist/continuity.js``. ``bun test`` imports ``./continuity.js`` which bun
resolves to the fixed ``.ts`` SOURCE, so the TS suite is green while the
shipped artifact is stale. That is exactly how the cross-project file drop
shipped as a no-op for
OpenClaw: the ``crossProjectFileDrop`` logic lived in ``src/continuity.ts``
but never made it into ``dist/continuity.js``.

This test fails the build when the shipped dist drifts from src. It does not
need tsc/bun (CI's pytest leg has neither), so it gates every push. Two bars:

  1. Symbol presence — the cross-project file-drop markers must appear in ``dist/continuity.js``:
       * ``crossProjectFileDrop`` (the file-filter function)
       * the disclosure string "scoped to current project"
     A stale dist (the C1 regression) is missing both.

  2. File coverage — every non-test ``src/*.ts`` must have a compiled
     ``dist/*.js``. A new source file added without ``bun run build`` fails
     here instead of shipping an unresolved ``require``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OPENCLAW = REPO / "openclaw"
SRC = OPENCLAW / "src"
DIST = OPENCLAW / "dist"


def test_dist_continuity_carries_103_markers() -> None:
    """The shipped dist/continuity.js must contain the cross-project file-drop logic, not just src."""
    dist_continuity = DIST / "continuity.js"
    assert dist_continuity.is_file(), (
        f"missing shipped artifact: {dist_continuity} (run `bun run build` in openclaw/)"
    )
    text = dist_continuity.read_text(encoding="utf-8")
    assert "crossProjectFileDrop" in text, (
        "dist/continuity.js is stale: crossProjectFileDrop (the file filter) "
        "is in src/continuity.ts but missing from the shipped dist. "
        "Run `bun run build` in openclaw/ and commit the regenerated dist."
    )
    assert "scoped to current project" in text, (
        "dist/continuity.js is stale: the disclosure string is in "
        "src/continuity.ts but missing from the shipped dist. "
        "Run `bun run build` in openclaw/ and commit the regenerated dist."
    )


def test_dist_covers_every_non_test_source_file() -> None:
    """Every non-test src/*.ts must have a compiled dist/*.js sibling."""
    if not SRC.is_dir() or not DIST.is_dir():
        pytest.skip("openclaw src/ or dist/ not present in this checkout")
    missing: list[str] = []
    for src_file in sorted(SRC.glob("*.ts")):
        if src_file.suffix == ".ts" and src_file.stem.endswith(".test"):
            # Test files compile into dist too (tsconfig includes src/**/*),
            # but they are not load-bearing shipped artifacts — skip them so
            # the guard fires on real source drift, not test-only additions.
            continue
        compiled = DIST / (src_file.stem + ".js")
        if not compiled.is_file():
            missing.append(f"{src_file.name} -> {compiled.name} (absent)")
    assert not missing, (
        "openclaw dist/ is stale: source files without a compiled dist artifact "
        "(run `bun run build` in openclaw/ and commit the regenerated dist):\n  "
        + "\n  ".join(missing)
    )
