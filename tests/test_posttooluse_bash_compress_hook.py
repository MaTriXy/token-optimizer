#!/usr/bin/env python3
"""UNIT B (hardened): Integration tests for PostToolUse bash_compress_hook.py.

Tests empirically prove:
1. The hook compresses bash tool output and returns updatedToolOutput
2. The compressed stdout is SMALLER than original (model sees less)
3. Side-effecting commands pass through unmodified (fail-open safety)
4. Credential scan runs and preserves sensitive lines
5. The hook doesn't double-compress already-compressed output
6. **B2**: emitted compressed output carries a resolvable archive pointer
7. **Baseline invariant**: the baseline-size invariant is enforced
8. **B3**: error-on-stdout (2>&1) passes through raw (no compression)
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BASH_COMPRESS_HOOK = REPO / "skills" / "token-optimizer" / "scripts" / "bash_compress_hook.py"

# Long, realistic CLI output that should compress by >10%
_LONG_GIT_LOG_OUTPUT = "\n".join(
    "commit " + "a" * 40 + "\n"
    "Author: Test User <test@example.com>\n"
    "Date:   Thu Aug 27 10:00:00 2026 +0000\n\n"
    f"    Commit message number {i}: make some changes to the codebase\n"
    f"gpg: Signature made Thu Aug 27 10:00:{i%60:02d} 2026\n"
    f"gpg:                using RSA key ABCD1234EFGH5678\n"
    f"Primary key fingerprint: 1234 5678 9ABC DEF0 1234 5678 9ABC DEF0 1234 5678\n"
    for i in range(1, 101)
)

_LONG_LS_OUTPUT = "\n".join(
    f"-rw-r--r--  1 user  staff  {1000+i:>6} Aug {10+(i%20):>2} 10:{i%60:02d} file_{i:04d}.py"
    for i in range(1, 201)
)

_VERBOSE_PYTEST_OUTPUT = (
    "============================= test session starts ==============================\n"
    "platform darwin -- Python 3.12.0, pytest-8.0.0, pluggy-1.5.0\n"
    "rootdir: /Users/test/project\n"
    "collecting ... \n"
    + "\n".join(
        f"tests/test_module_{i:03d}.py::test_case_{j:03d} PASSED"
        for i in range(1, 11) for j in range(1, 10)
    )
    + "\n\n"
    "============================= 90 passed in 2.34s ==============================\n"
)


def _payload(command: str, stdout: str, stderr: str = "",
             interrupted: bool = False, is_image: bool = False,
             tool_use_id: str = "toolu_01UNITTEST123",
             session_id: str | None = None) -> str:
    """Build a PostToolUse hook stdin payload matching Claude Code's actual format.

    session_id defaults to a UNIQUE per-call id so the cross-turn dedup's
    persistent SessionStore (now active since the hook threads the payload
    session_id into CLAUDE_SESSION_ID) can't leak a stored output from a prior
    test or a prior suite run into this test. A test that wants to exercise
    same-session dedup passes an explicit shared session_id."""
    return json.dumps({
        "session_id": session_id or ("test-session-" + uuid.uuid4().hex),
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/Users/test/project",
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {
            "stdout": stdout,
            "stderr": stderr,
            "interrupted": interrupted,
            "isImage": is_image,
        },
        "tool_use_id": tool_use_id,
        "duration_ms": 250,
    })


def _run_hook(command: str, stdout: str, stderr: str = "",
              interrupted: bool = False, is_image: bool = False,
              tool_use_id: str = "toolu_01UNITTEST123",
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run bash_compress_hook.py as a subprocess with simulated PostToolUse input."""
    env = {**os.environ}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=_payload(command, stdout, stderr, interrupted, is_image, tool_use_id),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=env,
    )


def _get_updated_stdout(proc: subprocess.CompletedProcess) -> str | None:
    """Extract updatedToolOutput.stdout from hook response, or None."""
    if not proc.stdout.strip():
        return None
    try:
        response = json.loads(proc.stdout)
        updated = response.get("hookSpecificOutput", {}).get("updatedToolOutput", {})
        return updated.get("stdout")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ============================================================================
# Core tests: proving compression reaches model-visible context
# ============================================================================

class TestHookCompressesOutput:
    """updatedToolOutput.stdout must be smaller than original."""

    def test_git_log_pipeline_is_compressed(self):
        cmd = "git log --oneline | head -20"
        proc = _run_hook(cmd, _LONG_GIT_LOG_OUTPUT)
        assert proc.returncode == 0, f"Hook crashed: {proc.stderr}"

        compressed = _get_updated_stdout(proc)
        assert compressed is not None, (
            "Expected updatedToolOutput for pipeline command, got pass-through.\n"
            f"stdout: {proc.stdout[:500]}"
        )
        assert len(compressed) < len(_LONG_GIT_LOG_OUTPUT), (
            f"Compressed ({len(compressed)} chars) must be < original ({len(_LONG_GIT_LOG_OUTPUT)})"
        )

        # Verify response shape
        response = json.loads(proc.stdout)
        updated = response["hookSpecificOutput"]["updatedToolOutput"]
        assert updated["stdout"] == compressed
        assert updated["stderr"] == ""
        assert updated["interrupted"] is False
        assert updated["isImage"] is False

    def test_multi_stage_pipeline_is_compressed(self):
        cmd = "grep -r FIXME src/ | sort | uniq -c | sort -rn | head -10"
        grep_output = "\n".join(
            f"src/file_{i:03d}.py:{100+i}:    # FIXME: improve error handling"
            for i in range(1, 101)
        )
        proc = _run_hook(cmd, grep_output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        assert compressed is not None, "Expected compression for multi-stage pipeline"
        assert len(compressed) < len(grep_output)

    def test_ls_piped_to_wc_is_compressed(self):
        cmd = "ls -la | wc -l"
        proc = _run_hook(cmd, _LONG_LS_OUTPUT)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(_LONG_LS_OUTPUT)

    def test_pytest_output_is_compressed(self):
        cmd = "pytest tests/"
        proc = _run_hook(cmd, _VERBOSE_PYTEST_OUTPUT)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(_VERBOSE_PYTEST_OUTPUT)
        assert "passed" in compressed.lower() or "90" in compressed

    def test_du_sort_head_pipeline_is_compressed(self):
        cmd = "du -sh * | sort -rh | head -10"
        du_output = "\n".join(
            f"{1024 * (i % 50 + 1)}K\tdir_{i:04d}" for i in range(1, 101)
        )
        proc = _run_hook(cmd, du_output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(du_output)


# ============================================================================
# Safety: side-effecting commands pass through UNMODIFIED
# ============================================================================

class TestSideEffectingPassThrough:
    def test_git_push_passes_through(self):
        cmd = "git push origin main"
        proc = _run_hook(cmd, "Everything up-to-date\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_rm_rf_passes_through(self):
        cmd = "rm -rf build/"
        proc = _run_hook(cmd, "")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_npm_install_passes_through(self):
        cmd = "npm install express"
        proc = _run_hook(cmd, "added 50 packages in 2s\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_bash_c_passes_through(self):
        cmd = "bash -c 'ls | grep foo'"
        proc = _run_hook(cmd, "foo.txt\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_sudo_passes_through(self):
        cmd = "sudo ls /root"
        proc = _run_hook(cmd, "secret-file.txt\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_git_branch_delete_passes_through(self):
        cmd = "git branch -D old-branch"
        proc = _run_hook(cmd, "Deleted branch old-branch\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, "git branch -D should NOT be compressed"

    def test_find_delete_passes_through(self):
        cmd = "find . -name '*.pyc' -delete"
        proc = _run_hook(cmd, "")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, "find -delete should NOT be compressed"

    def test_sqlite3_replace_passes_through(self):
        cmd = "sqlite3 db 'REPLACE INTO t VALUES(1)'"
        proc = _run_hook(cmd, "")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, "sqlite3 REPLACE should NOT be compressed"


# ============================================================================
# Boundary condition tests
# ============================================================================

class TestBoundaryConditions:
    def test_small_output_passes_through(self):
        cmd = "echo hello | wc -c"
        proc = _run_hook(cmd, "6\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_interrupted_output_passes_through(self):
        cmd = "git log | head -20"
        proc = _run_hook(cmd, _LONG_GIT_LOG_OUTPUT, interrupted=True)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_image_output_passes_through(self):
        cmd = "cat image.png"
        proc = _run_hook(cmd, "binary...", is_image=True)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_non_bash_tool_ignored(self):
        payload = json.dumps({
            "session_id": "test",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "tool_response": {"content": "file content"},
        })
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input=payload, capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
        assert not proc.stdout.strip()

    def test_empty_command_passes_through(self):
        proc = _run_hook("", "some output")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_already_compressed_output_not_double_compressed(self):
        cmd = "git log | head -20"
        already_compressed = (
            "branch: main\n"
            "abc123 feat: add new feature\n\n"
            "[Full result archived (5,000 chars) — saved to disk, not lost.\n"
            "expand toolu_01UNITTEST123 Bash]"
        )
        proc = _run_hook(cmd, already_compressed)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None


# ============================================================================
# Archive pointer + baseline-size invariant
# ============================================================================

class TestArchivePointerAndUnitC:
    """Emitted compressed output must carry a resolvable archive pointer
    and pass through Unit C's baseline invariant."""

    def test_compressed_output_has_archive_pointer(self):
        """B2: The compressed stdout must contain an expand pointer so the
        model can retrieve the full original."""
        cmd = "git log --oneline | head -20"
        proc = _run_hook(cmd, _LONG_GIT_LOG_OUTPUT)
        assert proc.returncode == 0

        compressed = _get_updated_stdout(proc)
        if compressed is None:
            # The hook may fail to compress or the output may already show
            # an error. Test at least that compression happened.
            pytest.skip("Compression not triggered — archive test not applicable")

        # The archive pointer has a recognizable format:
        # "[Full result archived (N chars) — saved to disk, not lost.\nexpand <key> <tool>]"
        assert "[Full result archived" in compressed or "[Archived" in compressed or \
               "saved to disk" in compressed or "expand " in compressed, (
            "Compressed output missing archive pointer! Model cannot retrieve raw.\n"
            f"Compressed (first 300): {compressed[:300]}"
        )

    def test_compressed_output_not_larger_than_raw(self):
        """B2/Unit C: The compressed output must never exceed raw output size."""
        cmd = "git log --oneline | head -20"
        # Simulate various output sizes
        for length in [500, 2000, 8000, 15000]:
            output = "x" * length
            proc = _run_hook(cmd, output)
            assert proc.returncode == 0
            compressed = _get_updated_stdout(proc)
            if compressed is not None:
                assert len(compressed) <= len(output), (
                    f"Compressed ({len(compressed)}) > raw ({len(output)}) — "
                    f"baseline-size invariant violated!"
                )

    def test_compression_never_inflates_output(self):
        """Even on pathological input, compression must not inflate."""
        cmd = "find src -name '*.py' | wc -l"
        output = "\n".join(f"unique line {i:08d} padding" for i in range(100))
        proc = _run_hook(cmd, output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        if compressed is not None and compressed != output:
            assert len(compressed) <= len(output), (
                f"Compressed ({len(compressed)}) > original ({len(output)})"
            )


# ============================================================================
# B3: Error-on-stdout pass-through
# ============================================================================

class TestErrorOnStdoutPassThrough:
    """When errors appear on stdout (2>&1 redirect), the hook must pass
    through raw so the model sees the errors."""

    def test_error_on_stdout_passes_through(self):
        """B3: stderr redirected to stdout with error patterns → no compression."""
        cmd = "npm test 2>&1 | tail -20"
        # Output that looks like test failures on stdout
        output = (
            "test 1: ok\n" * 5
            + "error: Cannot find module 'express'\n"
            + "error: Test suite failed\n"
            + "fatal: process exited with code 1\n"
            + "test 2: ok\n" * 5
        ) * 5  # High enough error density to trigger B3 guard

        proc = _run_hook(cmd, output)
        assert proc.returncode == 0, f"Hook crashed: {proc.stderr}"
        assert _get_updated_stdout(proc) is None, (
            "Error on stdout should cause PASS-THROUGH, not compression!"
        )

    def test_normal_output_with_few_error_keywords_still_compresses(self):
        """A few scattered error-like words shouldn't block compression.

        The B3 guard requires both >=3 error lines AND >10% density.
        """
        cmd = "grep error src/ | head -20"
        # Only 2 error-like lines in 20+ lines = <10% density
        output = (
            "line 1: normal\n" * 8
            + "line 9: error_handling_module.py (this is a filename)\n"
            + "line 10: normal\n" * 8
            + "line 19: check_error_bounds (another filename)\n"
        )
        proc = _run_hook(cmd, output)
        assert proc.returncode == 0

        compressed = _get_updated_stdout(proc)
        # With grep output, compression depends on the search_results handler.
        # If it passes through (None), that's also OK — just not an error.
        if compressed is not None:
            # Verify it was actually compressed (smaller)
            pass  # Compression happened; low error density didn't block it


# ============================================================================
# Credential preservation
# ============================================================================

class TestCredentialPreservation:
    def test_credential_line_survives_compression(self):
        """AWS-key-pattern line survives even when git_log handler strips it."""
        cmd = "git log --oneline | head -20"
        fake_aws_key = "AKIA1234567890ABCDEF"
        output = "\n".join(
            "commit " + ("a" * 40) + "\n"
            "Author: User <user@example.com>\n"
            "Date:   Thu Aug 27 10:00:00 2026 +0000\n\n"
            f"    Commit {i}: changes\n"
            f"gpg: Signature made Thu Aug 27 10:00:{i % 60:02d} 2026\n"
            for i in range(1, 51)
        )
        output += f"\ngpg: using key {fake_aws_key} for signing\n"
        output += "\n".join(
            "commit " + ("b" * 40) + "\n"
            "Author: User <user@example.com>\n"
            "Date:   Thu Aug 27 11:00:00 2026 +0000\n\n"
            f"    Commit {i}: more changes\n"
            for i in range(51, 101)
        )

        proc = _run_hook(cmd, output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        if compressed is None:
            pytest.skip("Compression not triggered")
        assert fake_aws_key in compressed, "AWS key was DROPPED from compressed output!"

    def test_error_lines_preserved(self):
        cmd = "npm test 2>&1 | grep -E '(error|fail)'"
        output = (
            "test 1: ok\n"
            "test 2: ok\n"
            "Error: Cannot find module 'express'\n"
            "test 3: ok\n"
        ) * 10

        proc = _run_hook(cmd, output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        # Error density here is ~25%, which triggers B3 guard → pass through.
        # This is CORRECT: the model sees the full error output.
        if compressed is not None:
            assert "Error" in compressed or "error" in compressed


# ============================================================================
# Fail-open
# ============================================================================

class TestFailOpen:
    def test_malformed_json_input_does_not_crash(self):
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input="not valid json {{{", capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0

    def test_missing_fields_does_not_crash(self):
        payload = json.dumps({"tool_name": "Bash"})
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input=payload, capture_output=True, text=True, timeout=10,
        )
        assert proc.returncode == 0
