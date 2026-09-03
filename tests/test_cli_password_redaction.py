"""Inline password flags on command lines: what must be redacted and what must
be left alone.

The command line is persisted (streak store, nudge label), so a value that
escapes redaction lands on disk in plaintext. The negative cases matter as
much as the positive ones: ``-P`` is MySQL's port flag, ``--password-stdin``
and ``--password-file`` carry no value, and a bare ``-p`` means "prompt me".
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "skills", "token-optimizer", "scripts"))

import credential_patterns as cp  # noqa: E402


def _j(*parts: str) -> str:
    return "".join(parts)


REDACTED = [
    (_j('psql --password="s3 cret" -h db'), "s3 cret"),
    (_j("tool --password='qq' x"), "qq"),
    (_j("tool --password=plain"), "plain"),
    (_j("tool --password plain2"), "plain2"),
    (_j("tool --PASSWD=upper"), "upper"),
    (_j("cli --auth-token=abc.def"), "abc.def"),
    (_j("cli --passcode 123456"), "123456"),
    (_j('mysql -u root -p"quo ted" -e 1'), "quo ted"),
    (_j("MySQL -pSeCr -e 1"), "SeCr"),
    (_j("mysql -u root -pS3 -P 3306"), "S3"),
    (_j("mariadb -pmm -e 1"), "mm"),
    (_j("sshpass -p hunter ssh box"), "hunter"),
    (_j("sshpass -p 'h u' ssh box"), "h u"),
    (_j("redis-cli -a pw ping"), "pw"),
    (_j("redis-cli -h host -a 'p w' ping"), "p w"),
    (_j('mysql --password="long form" -e 1'), "long form"),
    (_j("mysql --password='lf2' -e 1"), "lf2"),
    (_j("mysql -u root --password lf3 -e 1"), "lf3"),
    (_j("mariadb --password=lf4"), "lf4"),
]

UNCHANGED = [
    "mysql -u root -P 3306 -e 1",
    "docker login --password-stdin",
    "tool --password-file /x/y",
    "mysql -u root -p -e 1",
    "sshpass -p",
    "grep -p pattern file",
    "curl -a x",
    "redis-cli -a",
    "tool --password",
    "mysql --password-stdin",
    "mysql --password-file /x/y",
    "mariadb --password-stdin",
    "sshpass --passcode-x",
]


@pytest.mark.parametrize("command,secret", REDACTED)
def test_inline_password_value_is_redacted(command, secret):
    out = cp.redact_credentials(command)
    assert secret not in out, out
    assert "[CREDENTIAL REDACTED:" in out, out


@pytest.mark.parametrize("command", UNCHANGED)
def test_flags_without_a_secret_are_untouched(command):
    assert cp.redact_credentials(command) == command


def test_port_flag_survives_next_to_a_password():
    out = cp.redact_credentials("mysql -u root -pS3 -P 3306 -e 1")
    assert "-P 3306" in out and "S3" not in out


def test_redaction_is_idempotent_on_quoted_values():
    once = cp.redact_credentials('psql --password="s3 cret" -h db')
    assert cp.redact_credentials(once) == once


def test_nudge_label_never_carries_the_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "label-redaction-test")
    import thrash_guard
    cmd = 'psql --password="s3 cret" -h db'
    nudge = None
    for _ in range(thrash_guard.STREAK_THRESHOLD):
        nudge = thrash_guard.check(cmd, "FATAL: password authentication failed", now=1000.0)
    assert nudge is not None
    assert "s3 cret" not in nudge
    assert "REDACTED" in nudge
