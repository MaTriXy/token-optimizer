"""The two asset trees must stay byte-identical too.

`skills/token-optimizer/assets/` and
`plugins/token-optimizer/skills/token-optimizer/assets/` ship the same files to
different install paths, exactly like the scripts/ trees guarded by
test_duplicate_script_sync.py. Nothing in the build copies one to the other at
edit time (the Codex sync only runs at release), so a fix applied to one copy
and not the other is invisible until the release gate rejects it -- or, worse,
until a user on the other install path sees a stale dashboard.

This is not hypothetical: in v5.11.79 the savings-card fix edited
skills/.../assets/dashboard.html but not its plugins/ mirror. The scripts-only
byte-identity test stayed green, and the drift was caught only by the
sign-release Codex-parity gate. dashboard.html is ~9k lines; that drift is
silent under a manual diff. This converts the invariant from discipline into a red test
that fails at edit time, before the tag.

Intentional one-sided files are listed in ONE_SIDED.
"""

import hashlib
import os

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE_A = os.path.join(REPO_ROOT, "skills", "token-optimizer", "assets")
TREE_B = os.path.join(
    REPO_ROOT, "plugins", "token-optimizer", "skills", "token-optimizer", "assets"
)

# Files that legitimately live in only one tree, relative to that tree's root.
ONE_SIDED: set = set()

# Never compared: regenerable artifacts and caches carry no source meaning.
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


def test_shared_assets_are_byte_identical():
    """A file present in both asset trees must be the same file in both trees."""
    shared = _relative_files(TREE_A) & _relative_files(TREE_B)
    assert shared, "found no shared asset files -- the tree paths are probably wrong"

    drifted = [
        rel
        for rel in sorted(shared)
        if _digest(os.path.join(TREE_A, rel)) != _digest(os.path.join(TREE_B, rel))
    ]
    assert not drifted, (
        "asset files drifted between the two install trees (edit both, or run "
        "scripts/sync-codex-marketplace-plugin.sh): " + ", ".join(drifted)
    )


def test_no_unmirrored_assets():
    """A file in one asset tree but not the other must be a deliberate ONE_SIDED entry."""
    only_a = _relative_files(TREE_A) - _relative_files(TREE_B) - ONE_SIDED
    only_b = _relative_files(TREE_B) - _relative_files(TREE_A) - ONE_SIDED
    assert not only_a, f"assets only in skills/ tree (add to plugins/ or ONE_SIDED): {sorted(only_a)}"
    assert not only_b, f"assets only in plugins/ tree (add to skills/ or ONE_SIDED): {sorted(only_b)}"
