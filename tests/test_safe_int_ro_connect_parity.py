"""Parity test: all copies of _safe_int and _ro_connect must use the same
hardened pattern, so a fix applied to one copy and not the others is a red
test.

H-1: _safe_int was fixed in 1 of 7 copies (copilot_vscode.py). The other 6
silently returned 0 for float-shaped strings like "1234.0", zeroing token
counts from those runtimes. This test scans every copy and verifies it
parses through float() first.

H-2: _ro_connect was fixed in 1 of 4 copies (codex_state.py). The other 3
used f"file:{path}?mode=ro" string interpolation, where a path containing
"?" could override mode=ro and open read-write. This test scans every
read-only SQLite connect site and verifies it uses Path.as_uri().

The test is structural (source scan, not import) so it catches a new copy
pasted with the old pattern even if no test imports that module.
"""
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(REPO_ROOT, "skills", "token-optimizer", "scripts")

# Files that contain a _safe_int definition.
_SAFE_INT_FILES = [
    "codex_session.py",
    "copilot_session.py",
    "copilot_state.py",
    "copilot_vscode.py",
    "cursor_session.py",
    "hermes_session.py",
    "hermes_state.py",
    "structure_replay.py",
]

# Files that contain a read-only SQLite connect via file: URI.
_RO_CONNECT_FILES = [
    "codex_state.py",
    "copilot_vscode.py",
    "cursor_state.py",
    "hermes_doctor.py",
    "hermes_state.py",
]


def _read(rel: str) -> str:
    with open(os.path.join(SCRIPTS, rel), "r", encoding="utf-8") as f:
        return f.read()


def test_safe_int_all_copies_parse_through_float():
    """H-1: every _safe_int copy must use int(float(value)), not int(value).

    int("1234.0") raises ValueError, silently returning the default (0) and
    zeroing token counts. int(float("1234.0")) correctly returns 1234.
    """
    missing = []
    for fname in _SAFE_INT_FILES:
        src = _read(fname)
        if "def _safe_int" not in src:
            missing.append(fname)
            continue
        # Extract the _safe_int function body (up to the next def or blank line
        # followed by a new top-level construct).
        match = re.search(r"def _safe_int\(.*?\n((?:    .*\n|    \n)+)", src)
        if not match:
            missing.append(f"{fname} (could not extract body)")
            continue
        body = match.group(1)
        if "int(float(" not in body:
            missing.append(f"{fname} (no int(float()) in body)")
        if "OverflowError" not in body:
            missing.append(f"{fname} (no OverflowError guard)")
    assert not missing, (
        "_safe_int copies missing the int(float()) + OverflowError fix:\n  "
        + "\n  ".join(missing)
        + "\n\nApply int(float(value)) and add OverflowError to the except tuple."
    )


def test_no_vulnerable_ro_connect_string_interpolation():
    """H-2: no read-only SQLite connect may use f"file:{path}" interpolation.

    A path containing ?, #, %, or spaces would break the URI: ? starts the
    query string early and could override mode=ro, opening read-write. Every
    read-only connect must build the URI via Path.as_uri() which
    percent-encodes those characters.
    """
    violations = []
    for fname in _RO_CONNECT_FILES:
        src = _read(fname)
        # Look for the vulnerable pattern: f"file:{...}?mode=ro or
        # "file:{path}?mode=ro without as_uri. The fixed pattern uses
        # Path(path).as_uri() or Path(db_path).as_uri() to build the URI.
        for line_num, line in enumerate(src.splitlines(), 1):
            stripped = line.strip()
            # Skip comments and docstrings.
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            # The vulnerable pattern: f"file:{...}?mode=ro or file:{path}?mode=ro
            # without as_uri in the same line or a preceding line.
            if re.search(r'["\']file:\{[^}]+\}\?mode=ro', stripped) or \
               re.search(r'["\']file:\{path\}', stripped):
                if "as_uri" not in stripped:
                    # Check if as_uri is on a nearby line (within 3 lines).
                    lines = src.splitlines()
                    nearby = " ".join(
                        lines[max(0, line_num - 4):line_num + 1])
                    if "as_uri" not in nearby:
                        violations.append(f"{fname}:{line_num}: {stripped}")
    assert not violations, (
        "Read-only SQLite connects using vulnerable string interpolation "
        "(missing Path.as_uri()):\n  "
        + "\n  ".join(violations)
        + "\n\nUse Path(path).as_uri() to build the file: URI so ?, #, %, "
        "and spaces are percent-encoded."
    )


# ---------------------------------------------------------------------------
# M-7: parametrized tests for _safe_int float-string parsing.
# Imports each module and calls _safe_int directly.
# ---------------------------------------------------------------------------
import importlib
import sys
import pytest

sys.path.insert(0, SCRIPTS)


@pytest.mark.parametrize("fname,has_default", [
    ("codex_session.py", False),
    ("copilot_session.py", True),
    ("copilot_state.py", True),
    ("copilot_vscode.py", True),
    ("cursor_session.py", True),
    ("hermes_session.py", True),
    ("hermes_state.py", True),
    ("structure_replay.py", True),
])
def test_safe_int_parses_float_string(fname, has_default):
    """M-7: _safe_int("1234.0") must return 1234, not 0 (the default).

    This is the exact regression H-1 fixed: int("1234.0") raises ValueError,
    silently returning 0 and zeroing token counts from runtimes that export
    float-shaped strings in JSON.
    """
    mod_name = fname.replace(".py", "")
    if mod_name in sys.modules:
        mod = sys.modules[mod_name]
    else:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            pytest.skip(f"cannot import {mod_name} (missing dependency)")
    safe_int = getattr(mod, "_safe_int", None)
    if safe_int is None:
        pytest.skip(f"{mod_name} has no _safe_int")
    if has_default:
        assert safe_int("1234.0") == 1234
        assert safe_int("1234") == 1234
        assert safe_int(None) == 0
        assert safe_int("not a number") == 0
        assert safe_int(float("inf")) == 0
    else:
        # codex_session._safe_int has no default param, uses `value or 0`.
        assert safe_int("1234.0") == 1234
        assert safe_int("1234") == 1234
        assert safe_int(None) == 0
        assert safe_int("not a number") == 0
        assert safe_int(float("inf")) == 0
