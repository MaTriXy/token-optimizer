#!/usr/bin/env python3
"""TOML command-filter schema + loader.

Verifies that ``load_filters`` parses a valid TOML into the expected
normalized config, that the ``_is_safe_add`` gate rejects unsafe entries,
that ``exclude`` removes commands, and that missing/malformed files are
fail-open.

Run: python3 -m pytest tests/test_command_filters_toml.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import command_filters as cf  # noqa: E402


# ---------------------------------------------------------------------------
# _is_safe_add gate
# ---------------------------------------------------------------------------

def test_safe_add_accepts_read_only_command():
    assert cf._is_safe_add("cargo test", "pytest") is True


def test_safe_add_rejects_unknown_handler():
    assert cf._is_safe_add("cargo test", "nonexistent_handler") is False


def test_safe_add_rejects_shell_interpreter():
    assert cf._is_safe_add("bash -c 'echo hi'", "ls") is False
    assert cf._is_safe_add("python -c 'print(1)'", "ls") is False
    assert cf._is_safe_add("node -e 'console.log(1)'", "json") is False


def test_safe_add_rejects_sudo():
    assert cf._is_safe_add("sudo ls", "ls") is False
    assert cf._is_safe_add("su -c 'ls'", "ls") is False


def test_safe_add_rejects_dangerous_chars():
    assert cf._is_safe_add("ls; rm -rf /", "ls") is False
    assert cf._is_safe_add("ls | grep x", "ls") is False
    assert cf._is_safe_add("ls $(whoami)", "ls") is False


def test_safe_add_rejects_write_subcommand():
    assert cf._is_safe_add("rm -rf /tmp/x", "ls") is False
    assert cf._is_safe_add("chmod 777 /etc", "ls") is False


def test_safe_add_rejects_empty():
    assert cf._is_safe_add("", "pytest") is False
    assert cf._is_safe_add("cargo test", "") is False


# ---------------------------------------------------------------------------
# load_filters from a TOML file
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "command-filters.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_valid_toml_parses_into_config(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
my-tests = { command = "cargo test", handler = "pytest" }
my-lint = { command = "ruff check", handler = "lint" }

[filters.exclude]
commands = ["git status", "ls -la"]
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert len(config.adds) == 2
    assert config.adds[0].name == "my-tests"
    assert config.adds[0].command == "cargo test"
    assert config.adds[0].handler == "pytest"
    assert config.adds[1].name == "my-lint"
    assert len(config.excludes) == 2
    assert "git status" in config.excludes
    assert "ls -la" in config.excludes


def test_add_with_unknown_handler_skipped(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
bad = { command = "cargo test", handler = "nonexistent" }
good = { command = "ruff check", handler = "lint" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert len(config.adds) == 1
    assert config.adds[0].name == "good"


def test_add_with_sudo_skipped(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
dangerous = { command = "sudo ls", handler = "ls" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert len(config.adds) == 0


def test_add_with_interpreter_skipped(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
interp = { command = "bash -c 'echo hi'", handler = "ls" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert len(config.adds) == 0


def test_exclude_removes_command(tmp_path, monkeypatch):
    toml_content = """
[filters.exclude]
commands = ["git status"]
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert config.is_excluded("git status") is True
    assert config.is_excluded("git log") is False


def test_missing_file_returns_empty_config(monkeypatch, tmp_path):
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(tmp_path / "nonexistent.toml"))
    cf.reset_cache()
    config = cf.load_filters()
    assert config.adds == ()
    assert config.excludes == ()


def test_malformed_toml_returns_empty_config(tmp_path, monkeypatch):
    p = tmp_path / "bad.toml"
    p.write_text("this is not valid toml [[[", encoding="utf-8")
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert config.adds == ()
    assert config.excludes == ()


def test_env_override_honored(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
custom = { command = "cargo test", handler = "pytest" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    config = cf.load_filters()
    assert len(config.adds) == 1
    assert config.adds[0].name == "custom"


def test_load_filters_is_idempotent_and_no_writes(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
a = { command = "cargo test", handler = "pytest" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    before = {str(x) for x in tmp_path.rglob("*")}
    c1 = cf.load_filters()
    c2 = cf.load_filters()
    after = {str(x) for x in tmp_path.rglob("*")}
    assert c1 == c2  # idempotent
    assert before == after  # no new files created


# ---------------------------------------------------------------------------
# merge_filters + get_effective_filters
# ---------------------------------------------------------------------------

def test_merge_filters_produces_effective_filters(tmp_path, monkeypatch):
    toml_content = """
[filters.add]
my-test = { command = "cargo test", handler = "pytest" }

[filters.exclude]
commands = ["git status"]
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    eff = cf.get_effective_filters()
    assert len(eff.user_adds) == 1
    assert eff.user_adds[0].command == "cargo test"
    assert eff.is_user_excluded("git status") is True
    assert eff.is_user_excluded("git log") is False


def test_get_effective_filters_caches(monkeypatch, tmp_path):
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(tmp_path / "nonexistent.toml"))
    cf.reset_cache()
    e1 = cf.get_effective_filters()
    e2 = cf.get_effective_filters()
    assert e1 is e2  # same object (cached)


# ---------------------------------------------------------------------------
# CommandFilterConfig helpers
# ---------------------------------------------------------------------------

def test_find_add_matches_exact():
    config = cf.CommandFilterConfig(
        adds=(cf.AddEntry(name="x", command="cargo test", handler="pytest"),),
    )
    assert config.find_add("cargo test") is not None
    assert config.find_add("cargo test").handler == "pytest"
    assert config.find_add("ruff check") is None


def test_find_add_matches_glob():
    config = cf.CommandFilterConfig(
        adds=(cf.AddEntry(name="x", command="cargo test*", handler="pytest"),),
    )
    assert config.find_add("cargo test --release") is not None


# ---------------------------------------------------------------------------
# U9: bash_hook + bash_compress integration
# ---------------------------------------------------------------------------

def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "command-filters.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_bash_hook_user_add_whitelists_read_only_command(tmp_path, monkeypatch):
    """A user add of a read-only command (e.g. cargo test) causes
    _is_whitelisted to return True."""
    import bash_hook
    toml_content = """
[filters.add]
my-test = { command = "cargo test", handler = "pytest" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    # "cargo test" is already in the built-in compound whitelist, so test
    # with a command that is NOT built-in.
    toml_content2 = """
[filters.add]
custom = { command = "my-custom-runner", handler = "pytest" }
"""
    p2 = _write_toml(tmp_path, toml_content2)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p2))
    cf.reset_cache()
    assert bash_hook._is_whitelisted("my-custom-runner") is True


def test_bash_hook_user_exclude_removes_builtin(tmp_path, monkeypatch):
    """A user exclude of 'git status' makes _is_whitelisted return False."""
    import bash_hook
    toml_content = """
[filters.exclude]
commands = ["git status"]
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    assert bash_hook._is_whitelisted("git status") is False
    # Other git commands still whitelisted
    assert bash_hook._is_whitelisted("git log") is True


def test_bash_hook_user_add_sudo_never_honored(tmp_path, monkeypatch):
    """A user add of sudo ... is never honored (rejected by _is_safe_add in
    the loader, so it never reaches _is_whitelisted)."""
    import bash_hook
    toml_content = """
[filters.add]
dangerous = { command = "sudo ls", handler = "ls" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    assert bash_hook._is_whitelisted("sudo ls") is False


def test_bash_hook_dangerous_chars_still_rejected(tmp_path, monkeypatch):
    """Categorical exclusion: a command with ;|$ is rejected regardless of
    user add."""
    import bash_hook
    toml_content = """
[filters.add]
dangerous = { command = "ls; rm -rf /", handler = "ls" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    # The add entry itself is rejected by _is_safe_add, so it never reaches
    # the whitelist. And even if it did, _has_dangerous_chars fires first in
    # the caller.
    assert bash_hook._has_dangerous_chars("ls; rm -rf /") is True
    assert bash_hook._is_whitelisted("ls; rm -rf /") is False


def test_bash_hook_git_write_subcmd_still_excluded(tmp_path, monkeypatch):
    """Git write subcommands stay excluded even if the user adds 'git'."""
    import bash_hook
    toml_content = """
[filters.add]
git-add = { command = "git commit", handler = "git_status" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    # "git commit" contains a space but no dangerous chars; _is_safe_add
    # does not reject it on the command string alone (it's not an
    # interpreter/sudo/write-subcmd by argv[0]). However, the built-in
    # _GIT_WRITE_SUBCMDS check in _is_whitelisted fires first for git,
    # returning False before the user-add fallback is reached.
    assert bash_hook._is_whitelisted("git commit") is False


def test_bash_compress_user_add_dispatches_to_handler(tmp_path, monkeypatch):
    """A user add of a read-only command causes _detect_pattern to return
    the declared handler."""
    import bash_compress
    toml_content = """
[filters.add]
custom = { command = "my-custom-runner", handler = "pytest" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    assert bash_compress._detect_pattern("my-custom-runner") == "pytest"


def test_bash_compress_user_exclude_returns_none(tmp_path, monkeypatch):
    """A user exclude of 'git status' makes _detect_pattern return None
    (raw passthrough)."""
    import bash_compress
    toml_content = """
[filters.exclude]
commands = ["git status"]
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    assert bash_compress._detect_pattern("git status") is None
    # Other git commands still detected
    assert bash_compress._detect_pattern("git log") == "git_log"


def test_bash_compress_builtin_not_replaced_by_user_add(tmp_path, monkeypatch):
    """Built-in detection runs first, so a user add naming a command that
    already has a built-in handler does NOT replace it."""
    import bash_compress
    # "git status" already maps to "git_status" builtin. A user add mapping
    # it to "pytest" should NOT override the built-in.
    toml_content = """
[filters.add]
override = { command = "git status", handler = "pytest" }
"""
    p = _write_toml(tmp_path, toml_content)
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(p))
    cf.reset_cache()
    assert bash_compress._detect_pattern("git status") == "git_status"


def test_bash_hook_builtin_unchanged_when_toml_absent(monkeypatch, tmp_path):
    """Built-in behavior is unchanged when the TOML file is absent."""
    import bash_hook
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(tmp_path / "nonexistent.toml"))
    cf.reset_cache()
    assert bash_hook._is_whitelisted("git status") is True
    assert bash_hook._is_whitelisted("pytest") is True
    assert bash_hook._is_whitelisted("rm -rf /") is False


def test_bash_compress_builtin_unchanged_when_toml_absent(monkeypatch, tmp_path):
    """Built-in behavior is unchanged when the TOML file is absent."""
    import bash_compress
    monkeypatch.setenv(cf.ENV_FILTERS_PATH, str(tmp_path / "nonexistent.toml"))
    cf.reset_cache()
    assert bash_compress._detect_pattern("git status") == "git_status"
    assert bash_compress._detect_pattern("pytest") == "pytest"
    assert bash_compress._detect_pattern("rm -rf /") is None
