"""Tests for MEDIUM-severity gauntlet fixes.

M-2: codex_state._normalize_ms OverflowError on float('inf')
M-3: codex_state._ro_connect ValueError on relative paths
M-6: codex_state as_uri URI-injection test
M-12: missing credential patterns (mysql -p, PGPASSWORD=, AWS secret key)
M-15: _crossturn_dedup redaction failure is silent
M-16: Bearer pattern re-matches its own placeholder (idempotency)
"""
import os
import sqlite3
import sys
import tempfile
import pytest

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, SCRIPTS)


# ---------------------------------------------------------------------------
# M-2: _normalize_ms must not raise OverflowError on float('inf')
# ---------------------------------------------------------------------------
class TestNormalizeMsOverflow:
    def test_inf_updated_at_ms_returns_none(self):
        from codex_state import _normalize_ms
        assert _normalize_ms(float('inf'), None) is None

    def test_nan_updated_at_ms_returns_none(self):
        from codex_state import _normalize_ms
        assert _normalize_ms(float('nan'), None) is None

    def test_inf_updated_at_returns_none(self):
        from codex_state import _normalize_ms
        assert _normalize_ms(None, float('inf')) is None

    def test_normal_values_still_work(self):
        from codex_state import _normalize_ms
        # A value >= 1e12 is treated as ms
        assert _normalize_ms(1_600_000_000_000, None) == 1_600_000_000_000
        # A value < 1e12 is treated as seconds and rescaled
        assert _normalize_ms(1_600_000_000, None) == 1_600_000_000_000


# ---------------------------------------------------------------------------
# M-3: _ro_connect must not raise ValueError on relative paths
# ---------------------------------------------------------------------------
class TestRoConnectRelativePath:
    def test_relative_path_does_not_raise_valueerror(self, tmp_path):
        """M-3: a relative path to _ro_connect should not propagate ValueError.
        The fix resolves the path before calling as_uri()."""
        from codex_state import _ro_connect
        # Create a temp DB, then reference it by relative path.
        db = tmp_path / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (1)")
        conn.commit()
        conn.close()
        # Change to tmp_path so the relative path resolves correctly.
        old_cwd = os.getcwd()
        try:
            os.chdir(str(tmp_path))
            # This should not raise ValueError.
            with _ro_connect(Path("test.db")) as conn:
                row = conn.execute("SELECT id FROM x").fetchone()
                assert row[0] == 1
        finally:
            os.chdir(old_cwd)


# ---------------------------------------------------------------------------
# M-6: as_uri URI-injection tests — paths with URI-significant characters
# must open read-only, not interpret those characters as URI syntax that
# could override mode=ro. '#' is a URI fragment delimiter (legal in filenames
# on both POSIX and Windows). '?' is a URI query-string delimiter but is
# illegal in Windows filenames, so it is tested POSIX-only.
# ---------------------------------------------------------------------------
class TestAsUriInjection:
    def test_db_path_with_hash_opens_readonly(self, tmp_path):
        """M-6: a DB path containing '#' must open read-only, not interpret
        the '#' as a URI fragment delimiter that could truncate the path and
        drop mode=ro. '#' is legal in filenames on both POSIX and Windows."""
        from codex_state import _ro_connect
        db = tmp_path / "db#test.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (42)")
        conn.commit()
        conn.close()
        # _ro_connect must open this read-only. If as_uri() didn't
        # percent-encode the '#', it would start the fragment early, truncating
        # the path before mode=ro and potentially opening read-write.
        with _ro_connect(db) as conn:
            row = conn.execute("SELECT id FROM x").fetchone()
            assert row[0] == 42
            # Verify read-only: a write attempt must fail.
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO x VALUES (99)")

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="'?' is illegal in Windows filenames; tested via '#' instead",
    )
    def test_db_path_with_question_mark_opens_readonly(self, tmp_path):
        """M-6 (POSIX-only): a DB path containing '?' must open read-only, not
        interpret the '?' as a URI query string delimiter that could override
        mode=ro. '?' is illegal in Windows filenames so this test is skipped
        there; the '#' test covers the same URI-escaping fix cross-platform."""
        from codex_state import _ro_connect
        db = tmp_path / "db?test.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (42)")
        conn.commit()
        conn.close()
        with _ro_connect(db) as conn:
            row = conn.execute("SELECT id FROM x").fetchone()
            assert row[0] == 42
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("INSERT INTO x VALUES (99)")

    def test_db_path_with_space_opens_readonly(self, tmp_path):
        """M-6: a DB path containing spaces must open read-only."""
        from codex_state import _ro_connect
        db = tmp_path / "my database.sqlite"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE x (id INTEGER)")
        conn.execute("INSERT INTO x VALUES (1)")
        conn.commit()
        conn.close()
        with _ro_connect(db) as conn:
            row = conn.execute("SELECT id FROM x").fetchone()
            assert row[0] == 1


# ---------------------------------------------------------------------------
# M-12: missing credential patterns
# ---------------------------------------------------------------------------
class TestMissingCredentialPatterns:
    def test_mysql_inline_password_redacted(self):
        from credential_patterns import redact_credentials
        out = redact_credentials("mysql -u root -pSECRET123 -h localhost")
        assert "SECRET123" not in out
        assert "[CREDENTIAL REDACTED:" in out

    def test_pgpassword_env_redacted(self):
        from credential_patterns import redact_credentials
        out = redact_credentials("PGPASSWORD=secret123 psql -h localhost")
        assert "secret123" not in out
        assert "[CREDENTIAL REDACTED:" in out

    def test_mysql_pwd_env_redacted(self):
        from credential_patterns import redact_credentials
        out = redact_credentials("MYSQL_PWD=hiddenpass mysql -u root")
        assert "hiddenpass" not in out
        assert "[CREDENTIAL REDACTED:" in out

    def test_aws_secret_key_redacted(self):
        from credential_patterns import redact_credentials
        # 40-char base64 secret key with context prefix
        secret = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/XY"
        assert len(secret) == 40
        out = redact_credentials(f"aws_secret={secret}")
        assert secret not in out
        assert "[CREDENTIAL REDACTED:" in out

    def test_aws_secret_key_with_equals_prefix(self):
        from credential_patterns import redact_credentials
        secret = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789+/XY"
        assert len(secret) == 40
        out = redact_credentials(f"SecretAccessKey: {secret}")
        assert secret not in out


# ---------------------------------------------------------------------------
# M-15: _crossturn_dedup redaction failure must log exception type
# ---------------------------------------------------------------------------
class TestCrossturnDedupFailureLogging:
    def test_redaction_failure_logs_exception_type(self, capsys, monkeypatch):
        """M-15: if _crossturn_dedup fails, the exception type must be logged
        to stderr so an admin can distinguish 'no prior run' from 'redaction
        failed'."""
        from bash_compress_hook import _crossturn_dedup
        # Force a failure by making redact_credentials raise.
        import credential_patterns
        def boom(text):
            raise RuntimeError("regex catastrophic backtracking")
        monkeypatch.setattr(credential_patterns, "redact_credentials", boom)
        # Set env vars so the function gets past the early exit.
        monkeypatch.setenv("CLAUDE_SESSION_ID", "test-session")
        # Large enough output to pass the len check.
        output = "x" * 300
        result = _crossturn_dedup("some command", output)
        assert result is None  # fail-open
        captured = capsys.readouterr()
        assert "[token optimizer]" in captured.err.lower()
        assert "runtimeerror" in captured.err.lower()


# ---------------------------------------------------------------------------
# M-16: Bearer pattern idempotency — re-redacting must not nest placeholders
# ---------------------------------------------------------------------------
class TestBearerIdempotency:
    def test_bearer_placeholder_not_re_redacted(self):
        """M-16: redacting text containing a Bearer placeholder must not
        re-match the 'Bearer token' text inside the placeholder."""
        from credential_patterns import redact_credentials
        text = "token=[CREDENTIAL REDACTED: Bearer token]"
        out = redact_credentials(text)
        assert out == text, (
            f"placeholder was re-redacted: {out}"
        )

    def test_bearer_idempotent_on_real_credential(self):
        """M-16: redacting real Bearer text twice must not nest."""
        from credential_patterns import redact_credentials
        text = "Authorization: Bearer abc123def456ghi789jkl012mno345pqr789"
        once = redact_credentials(text)
        twice = redact_credentials(once)
        assert once == twice, (
            f"not idempotent:\n  once:  {once}\n  twice: {twice}"
        )
        assert twice.count("[CREDENTIAL REDACTED") == 1


# Need Path import for M-3 test
from pathlib import Path
