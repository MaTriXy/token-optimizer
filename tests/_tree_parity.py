"""Shared helper: compare two directory trees by content, without touching either.

Both committed mirrors in this repo (``plugins/token-optimizer/`` for Codex and
``cowork/token-optimizer/`` for Cowork) are GENERATED from the canonical repo-root
content. The anti-drift tests used to prove reproducibility by regenerating the
mirror IN PLACE and asserting ``git status`` was clean. That worked, but it made
the test suite a writer: running the suite silently rewrote ~250 tracked files,
discarding any uncommitted edit an engineer had made to a mirror and deleting
files the generator excludes. A green ``git status`` afterwards made the loss
invisible.

The tests now regenerate into a throwaway staging root and compare the RESULT
against the committed tree with these helpers. Same guarantee ("the committed
mirror is exactly what the generator produces"), strictly better reporting
(per-file drift instead of a porcelain blob), no git dependency, and it works on
a dirty tree because it never writes to one.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

# Build artifacts and OS junk are regenerated per machine and carry no source
# meaning. The generators strip these too, so ignoring them here keeps the
# staged copy byte-equivalent to what the generator would have produced.
IGNORED_DIRS = {"__pycache__", ".pytest_cache"}
IGNORED_SUFFIXES = (".pyc", ".pyo")
IGNORED_NAMES = {".DS_Store"}

COPY_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store")


def relative_files(root: Path) -> set[str]:
    """Every meaningful file under ``root``, relative to it (POSIX separators)."""
    found: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for name in filenames:
            if name.endswith(IGNORED_SUFFIXES) or name in IGNORED_NAMES:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            found.add(Path(rel).as_posix())
    return found


def digest(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stage_inputs(stage: Path, repo: Path, entries) -> Path:
    """Copy ``entries`` (paths relative to ``repo``) into ``stage``, preserving layout.

    Missing entries are skipped, mirroring the generators' own tolerance for an
    absent optional input.
    """
    stage.mkdir(parents=True, exist_ok=True)
    for rel in entries:
        src = repo / rel
        if not src.exists():
            continue
        dst = stage / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, ignore=COPY_IGNORE)
        else:
            shutil.copy2(src, dst)
    return stage


def tree_drift(expected: Path, actual: Path) -> list[str]:
    """Human-readable drift lines between a freshly generated tree and a committed one.

    ``expected`` is what the generator just produced (in a staging dir);
    ``actual`` is the committed tree in the repo. Empty list means byte parity.
    """
    exp_files = relative_files(expected)
    act_files = relative_files(actual)

    lines: list[str] = []
    for rel in sorted(exp_files - act_files):
        lines.append(f"  MISSING from committed tree (generator produces it): {rel}")
    for rel in sorted(act_files - exp_files):
        lines.append(f"  EXTRA in committed tree (generator does not produce it): {rel}")
    for rel in sorted(exp_files & act_files):
        if digest(expected / rel) != digest(actual / rel):
            lines.append(f"  CONTENT differs from generator output: {rel}")
    return lines


def fingerprint(root: Path) -> dict:
    """path -> (sha256, inode, mtime_ns) for every file under ``root``.

    Used by the mirror tests to assert, in-test, that they did not write to the
    real tree -- inode/mtime catch a content-identical rewrite that a hash alone
    would miss (exactly how the old in-place rebuild hid on a clean tree).
    """
    out = {}
    for rel in relative_files(root):
        p = root / rel
        st = p.lstat()
        out[rel] = (digest(p), st.st_ino, st.st_mtime_ns)
    return out
