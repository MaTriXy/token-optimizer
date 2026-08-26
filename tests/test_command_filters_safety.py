#!/usr/bin/env python3
"""command-filter add-gate hardening + handler parity.

fix-7: _is_safe_add normalizes argv[0] with basename (so /bin/bash, ./python,
/usr/bin/sudo are caught), resolves a leading `env` (VAR=val + options), and
rejects bare/overly-broad wildcard patterns (just * or **). This is
defense-in-depth correctness (dangerous chars are blocked upstream), making the
gate deliver what its docstring claims.

fix-8: VALID_HANDLERS must equal the set of bash_compress._PATTERN_HANDLERS so a
future handler addition can't silently reject a user's valid config.

Run: python3 -m pytest tests/test_command_filters_safety.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import command_filters as cf  # noqa: E402


# ---------------------------------------------------------------------------
# fix-8: handler parity
# ---------------------------------------------------------------------------

def test_valid_handlers_matches_pattern_handlers():
    import bash_compress
    assert set(cf.VALID_HANDLERS) == set(bash_compress._PATTERN_HANDLERS), (
        "VALID_HANDLERS drifted from bash_compress._PATTERN_HANDLERS; a user "
        "config referencing a real handler would be silently rejected"
    )


# ---------------------------------------------------------------------------
# fix-7: interpreters / privilege escalation caught through path + env
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", [
    "/bin/bash -c ls",
    "./python script.py",
    "/usr/bin/python3 x.py",
    "/usr/bin/sudo ls",
    "/sbin/su - root",
    "env bash",
    "env PATH=/x python x.py",
    "env -i node app.js",
    "env -u HOME sudo ls",
    "env FOO=1 BAR=2 /bin/sh -c ls",
])
def test_unsafe_commands_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


@pytest.mark.parametrize("command", [
    "cargo test",
    "ruff check",
    "env FOO=1 cargo test",
    "ls -la",
    "/usr/local/bin/rg pattern",   # ripgrep by absolute path is read-only
])
def test_safe_commands_accepted(command):
    # Use a handler the command plausibly maps to; the point is the gate does
    # NOT reject an otherwise read-only command.
    assert cf._is_safe_add(command, "ls") is True, command


# ---------------------------------------------------------------------------
# fix-7: bare / overly-broad wildcards rejected
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("command", ["*", "**", "***", "* *"])
def test_wildcard_only_patterns_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


def test_wildcard_within_command_still_allowed():
    # A wildcard as an ARGUMENT (not the whole pattern / not argv[0]) is fine.
    assert cf._is_safe_add("ls *.py", "ls") is True


# ---------------------------------------------------------------------------
# fix-7: unknown handler and write subcommands still rejected
# ---------------------------------------------------------------------------

def test_unknown_handler_rejected():
    assert cf._is_safe_add("cargo test", "not_a_handler") is False


@pytest.mark.parametrize("command", ["rm -rf /tmp/x", "mv a b", "chmod 777 f"])
def test_write_subcommands_rejected(command):
    assert cf._is_safe_add(command, "ls") is False, command


# ---------------------------------------------------------------------------
# End-to-end: an unsafe add entry is dropped by the loader
# ---------------------------------------------------------------------------

def test_loader_drops_unsafe_add_entry(tmp_path, monkeypatch):
    toml = tmp_path / "command-filters.toml"
    toml.write_text(
        "[filters.add]\n"
        'sneaky = { command = "/bin/bash -c evil", handler = "ls" }\n'
        'good = { command = "cargo test", handler = "pytest" }\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(toml))
    cf.reset_cache()
    config = cf.load_filters()
    names = {a.name for a in config.adds}
    assert "good" in names
    assert "sneaky" not in names, "path-hidden interpreter must be gated out"
