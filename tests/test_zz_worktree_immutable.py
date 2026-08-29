#!/usr/bin/env python3
"""The suite must not write to the working tree. Named test, sorts last.

``tests/conftest.py`` fingerprints every git-TRACKED file at session start; this
file compares against that baseline at the end. The filename starts with ``zz``
so pytest's alphabetical file collection puts it after every other test file --
whatever ran before it is inside the window this checks.

Two layers on purpose:

  * this test, so a mutation shows up as a FAILED test with a name and a per-file
    report, and so ``pytest tests/test_zz_worktree_immutable.py`` is a thing you
    can run; and
  * ``pytest_sessionfinish`` in conftest.py, which fails the session regardless of
    collection or ordering. Ordering-dependent guards are how the original bug
    hid: a parity test passed in the full suite only because an earlier test had
    rewritten the tree under it.

Why it fingerprints ``(sha256, inode, mtime_ns)`` rather than content alone: the
regeneration that ate an engineer's edit produced BYTE-IDENTICAL output on a
clean tree. A hash-only guard is green on every machine except the one with
uncommitted work, which is the only machine that matters.

Run: python3 -m pytest tests/test_zz_worktree_immutable.py -q
"""

from __future__ import annotations

import pytest

import conftest as guard


def test_suite_did_not_mutate_any_tracked_file():
    if guard.BASELINE is None:
        pytest.skip(guard.UNAVAILABLE_REASON or "working-tree guard unavailable")

    mutations = guard.mutations_since_session_start()
    assert not mutations, (
        "the test suite mutated tracked files in the working tree:\n"
        + "\n".join(mutations)
        + "\n"
        + guard.FAILURE_ADVICE
    )


def test_guard_detects_a_content_identical_rewrite(tmp_path):
    """Negative test: the guard itself must fail on a rewrite, not just an edit.

    A guard that only ever passes is indistinguishable from a guard that cannot
    fail, so exercise the detector on a file rewritten with the same bytes -- the
    exact shape of the regeneration that silently discarded real work.
    """
    target = tmp_path / "mirrored.py"
    target.write_text("print('x')\n", encoding="utf-8")

    rel = str(target)
    before = guard.fingerprint([rel])

    # Regenerate in place: identical content, new inode (the copy-and-move a
    # generator does), so only an identity-aware fingerprint notices.
    replacement = tmp_path / "staged.py"
    replacement.write_text("print('x')\n", encoding="utf-8")
    replacement.replace(target)

    after = guard.fingerprint([rel])
    assert before[rel][0] == after[rel][0], "content must be identical for this test"

    changed = guard.diff_fingerprints(before, after)
    assert changed, "the guard missed a content-identical rewrite"
    assert "REWRITTEN" in changed[0], changed


def test_guard_detects_deletion_and_modification(tmp_path):
    """The other two shapes: a deleted file and an edited one."""
    edited = tmp_path / "edited.txt"
    deleted = tmp_path / "deleted.txt"
    edited.write_text("a\n", encoding="utf-8")
    deleted.write_text("b\n", encoding="utf-8")

    keys = [str(edited), str(deleted)]
    before = guard.fingerprint(keys)

    edited.write_text("a changed\n", encoding="utf-8")
    deleted.unlink()

    changed = guard.diff_fingerprints(before, guard.fingerprint(keys))
    kinds = {line.split()[0] for line in changed}
    assert kinds == {"MODIFIED", "DELETED"}, changed
