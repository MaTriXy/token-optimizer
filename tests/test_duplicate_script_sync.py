"""The two script trees must stay byte-identical.

`skills/token-optimizer/scripts/` and
`plugins/token-optimizer/skills/token-optimizer/scripts/` ship the same files to
different install paths. Nothing in the build copies one to the other, so a fix
applied to one copy and not the other is invisible until a user on the other
install path reports the bug a second time. measure.py alone is ~35k lines;
that drift is silent under a manual diff.

This converts the invariant from discipline into a red test.

Intentional one-sided files are listed in ONE_SIDED. Adding a file to only one
tree is a deliberate act, so it must be a deliberate edit here too -- otherwise
a file silently missing from an install path reads as "not duplicated yet"
rather than as a bug.
"""

import hashlib
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE_A = os.path.join(REPO_ROOT, "skills", "token-optimizer", "scripts")
TREE_B = os.path.join(
    REPO_ROOT, "plugins", "token-optimizer", "skills", "token-optimizer", "scripts"
)

# Files that legitimately live in only one tree, relative to that tree's root.
# benchmark.py is a development harness, not shipped to the plugin install path.
ONE_SIDED = {"benchmark.py"}

# Never compared: build artifacts and caches are regenerated per-machine and
# carry no source meaning.
IGNORED_DIR_PARTS = {"__pycache__", ".pytest_cache"}


def _relative_files(root):
    """Every real file under root, relative to it, minus regenerable artifacts."""
    found = set()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIR_PARTS]
        for name in filenames:
            if name.endswith(".pyc"):
                continue
            found.add(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def test_shared_scripts_are_byte_identical():
    """A file present in both trees must be the same file in both trees."""
    shared = _relative_files(TREE_A) & _relative_files(TREE_B)
    assert shared, "found no shared files -- the tree paths are probably wrong"

    drifted = [
        rel
        for rel in sorted(shared)
        if _digest(os.path.join(TREE_A, rel)) != _digest(os.path.join(TREE_B, rel))
    ]

    assert not drifted, (
        "These files differ between the two script trees:\n  "
        + "\n  ".join(drifted)
        + "\n\nApply the change to BOTH copies:\n"
        f"  {os.path.relpath(TREE_A, REPO_ROOT)}/<file>\n"
        f"  {os.path.relpath(TREE_B, REPO_ROOT)}/<file>"
    )


def test_one_sided_files_are_declared():
    """A file in only one tree must be an explicitly declared exception.

    Catches the other half of the drift class: not a changed file, but a NEW
    file added to one install path and forgotten in the other.
    """
    only_a = _relative_files(TREE_A) - _relative_files(TREE_B)
    only_b = _relative_files(TREE_B) - _relative_files(TREE_A)
    undeclared = sorted((only_a | only_b) - ONE_SIDED)

    assert not undeclared, (
        "These files exist in only one script tree:\n  "
        + "\n  ".join(undeclared)
        + "\n\nEither copy them to the other tree, or add them to ONE_SIDED in "
        "this test to record that the asymmetry is deliberate."
    )
