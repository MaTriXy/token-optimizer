#!/usr/bin/env python3
"""UNIT B: Integration tests for PostToolUse bash_compress_hook.py.

These tests prove empirically that:
1. The hook compresses bash tool output and returns updatedToolOutput
2. The compressed stdout is SMALLER than the original (the model sees less)
3. Side-effecting commands pass through unmodified (fail-open safety)
4. The credential/secret scan runs and preserves sensitive lines
5. The hook doesn't interfere with already-compressed output (no double-compress)

Each test simulates the real hook stdin → hook → assert the returned JSON
contains updatedToolOutput with compressed stdout, matching exactly what
Claude Code would read and pass to the model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
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
             interrupted: bool = False, is_image: bool = False) -> str:
    """Build a PostToolUse hook stdin payload matching Claude Code's actual format."""
    return json.dumps({
        "session_id": "test-session-unit-b",
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
        "tool_use_id": "toolu_01UNITTEST123",
        "duration_ms": 250,
    })


def _run_hook(command: str, stdout: str, stderr: str = "",
              interrupted: bool = False, is_image: bool = False,
              extra_env: dict | None = None) -> subprocess.CompletedProcess:
    """Run bash_compress_hook.py with a simulated PostToolUse payload.

    Returns the subprocess result. On success, stdout contains the JSON
    updatedToolOutput response. On pass-through, stdout is empty.
    """
    env = {**os.environ}
    env.pop("CLAUDE_PLUGIN_ROOT", None)  # Don't let plugin-root checks interfere
    env["CLAUDE_CONFIG_DIR"] = str(Path.home() / ".claude")
    if extra_env:
        env.update(extra_env)

    return subprocess.run(
        [sys.executable, str(BASH_COMPRESS_HOOK)],
        input=_payload(command, stdout, stderr, interrupted, is_image),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        env=env,
    )


def _get_updated_stdout(proc: subprocess.CompletedProcess) -> str | None:
    """Extract updatedToolOutput.stdout from hook response, or None if no compression."""
    if not proc.stdout.strip():
        return None
    try:
        response = json.loads(proc.stdout)
        updated = response.get("hookSpecificOutput", {}).get("updatedToolOutput", {})
        return updated.get("stdout")
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


# ============================================================================
# Core tests: proving compression reaches the model-visible context
# ============================================================================

class TestHookCompressesOutput:
    """Prove that updatedToolOutput.stdout is smaller than the original."""

    def test_git_log_pipeline_is_compressed(self, tmp_path):
        """git log | head: previously excluded by metachars, now compressible."""
        cmd = "git log --oneline | head -20"
        proc = _run_hook(cmd, _LONG_GIT_LOG_OUTPUT)
        assert proc.returncode == 0, f"Hook crashed: {proc.stderr}"

        compressed = _get_updated_stdout(proc)
        assert compressed is not None, (
            "Expected updatedToolOutput for pipeline command, got pass-through.\n"
            f"stdout: {proc.stdout[:500]}"
        )

        assert len(compressed) < len(_LONG_GIT_LOG_OUTPUT), (
            f"Compressed output ({len(compressed)} chars) must be smaller than "
            f"original ({len(_LONG_GIT_LOG_OUTPUT)} chars)"
        )

        # Verify the response has the correct shape
        response = json.loads(proc.stdout)
        updated = response["hookSpecificOutput"]["updatedToolOutput"]
        assert updated["stdout"] == compressed
        assert updated["stderr"] == ""  # stderr preserved as-is
        assert updated["interrupted"] is False
        assert updated["isImage"] is False

    def test_multi_stage_pipeline_is_compressed(self, tmp_path):
        """Multi-stage pipe: grep | sort | uniq -c | sort -rn | head."""
        cmd = "grep -r FIXME src/ | sort | uniq -c | sort -rn | head -10"

        # Create output that looks like grep results
        grep_output = "\n".join(
            f"src/file_{i:03d}.py:{100+i}:    # FIXME: improve error handling here"
            for i in range(1, 101)
        )

        proc = _run_hook(cmd, grep_output)
        assert proc.returncode == 0, f"Hook crashed: {proc.stderr}"

        compressed = _get_updated_stdout(proc)
        assert compressed is not None, (
            "Expected compression for multi-stage pipeline"
        )
        assert len(compressed) < len(grep_output), "Must shrink"

    def test_ls_piped_to_wc_is_compressed(self, tmp_path):
        """ls -la | wc -l: simple pipe, should compress."""
        cmd = "ls -la | wc -l"
        proc = _run_hook(cmd, _LONG_LS_OUTPUT)
        assert proc.returncode == 0

        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(_LONG_LS_OUTPUT)

    def test_pytest_output_is_compressed(self, tmp_path):
        """pytest with verbose output: should get summary compression."""
        cmd = "pytest tests/"
        proc = _run_hook(cmd, _VERBOSE_PYTEST_OUTPUT)
        assert proc.returncode == 0

        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(_VERBOSE_PYTEST_OUTPUT)

        # Pytest compression should keep the summary line
        assert "passed" in compressed.lower() or "90" in compressed, (
            "Compressed pytest output should preserve the pass count"
        )

    def test_du_sort_head_pipeline_is_compressed(self, tmp_path):
        """du -sh * | sort -rh | head -10: disk usage pipeline."""
        cmd = "du -sh * | sort -rh | head -10"
        du_output = "\n".join(
            f"{1024 * (i % 50 + 1)}K\tdir_{i:04d}"
            for i in range(1, 101)
        )
        proc = _run_hook(cmd, du_output)
        assert proc.returncode == 0

        compressed = _get_updated_stdout(proc)
        assert compressed is not None
        assert len(compressed) < len(du_output)


# ============================================================================
# Safety tests: side-effecting commands must pass through UNMODIFIED
# ============================================================================

class TestSideEffectingPassThrough:
    """Side-effecting commands must NOT be compressed (fail-open safety)."""

    def test_git_push_passes_through(self, tmp_path):
        cmd = "git push origin main"
        proc = _run_hook(cmd, "Everything up-to-date\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "git push should NOT be compressed"
        )

    def test_rm_rf_passes_through(self, tmp_path):
        cmd = "rm -rf build/"
        proc = _run_hook(cmd, "")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_npm_install_passes_through(self, tmp_path):
        cmd = "npm install express"
        proc = _run_hook(cmd, "added 50 packages in 2s\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "npm install should NOT be compressed (has side effects)"
        )

    def test_bash_c_passes_through(self, tmp_path):
        cmd = "bash -c 'ls | grep foo'"
        proc = _run_hook(cmd, "foo.txt\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "bash -c should NEVER be compressed"
        )

    def test_sudo_passes_through(self, tmp_path):
        cmd = "sudo ls /root"
        proc = _run_hook(cmd, "secret-file.txt\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_mixed_pipeline_with_write_rejected(self, tmp_path):
        """git add . && git status: git add is a write, so reject whole pipeline."""
        cmd = "git add . && git status"
        stdout = "On branch main\nnothing to commit, working tree clean\n"
        proc = _run_hook(cmd, stdout)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "Pipeline with git add should NOT be compressed"
        )


# ============================================================================
# Boundary condition tests
# ============================================================================

class TestBoundaryConditions:
    def test_small_output_passes_through(self, tmp_path):
        """Output < 100 chars should not be compressed."""
        cmd = "echo hello | wc -c"
        proc = _run_hook(cmd, "6\n")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_interrupted_output_passes_through(self, tmp_path):
        cmd = "git log | head -20"
        proc = _run_hook(cmd, _LONG_GIT_LOG_OUTPUT, interrupted=True)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "Interrupted commands must pass through"
        )

    def test_image_output_passes_through(self, tmp_path):
        cmd = "cat image.png"
        proc = _run_hook(cmd, "binary data...", is_image=True)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_non_bash_tool_ignored(self, tmp_path):
        """Hook should ignore non-Bash tool calls."""
        payload = json.dumps({
            "session_id": "test",
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "tool_response": {"content": "file content"},
        })
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0
        assert not proc.stdout.strip()  # No output = pass through

    def test_empty_command_passes_through(self, tmp_path):
        proc = _run_hook("", "some output")
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None

    def test_already_compressed_output_not_double_compressed(self, tmp_path):
        """Output carrying archive pointer should not be compressed again."""
        cmd = "git log | head -20"
        already_compressed = (
            "branch: main\n"
            "abc123 feat: add new feature\n\n"
            "[Full result archived (5,000 chars) — saved to disk, not lost.\n"
            "expand toolu_01UNITTEST123 Bash]"
        )
        proc = _run_hook(cmd, already_compressed)
        assert proc.returncode == 0
        assert _get_updated_stdout(proc) is None, (
            "Already-compressed output must NOT be double-compressed"
        )


# ============================================================================
# Credential/secret preservation test
# ============================================================================

class TestCredentialPreservation:
    def test_credential_line_survives_compression(self, tmp_path):
        """A line matching credential patterns survives compression.

        The PRE-compression token scan in bash_compress.py preserves lines
        matching known credential patterns even when the compression handler
        would normally drop them. We embed an AWS-key-pattern-matching line
        inside a gpg: line (which git_log compression strips). The preservation
        scan runs first, flags the line, and re-injects it after compression.
        """
        cmd = "git log --oneline | head -20"
        # AKIA... matches the AWS access key pattern in credential_patterns.py
        fake_aws_key = "AKIA1234567890ABCDEF"
        output = "\n".join(
            "commit " + ("a" * 40) + "\n"
            "Author: User <user@example.com>\n"
            "Date:   Thu Aug 27 10:00:00 2026 +0000\n\n"
            f"    Commit {i}: changes\n"
            f"gpg: Signature made Thu Aug 27 10:00:{i % 60:02d} 2026\n"
            for i in range(1, 51)
        )
        # Embed the AWS key in a line that git_log would DROP (starts with "gpg:")
        # but the PRE-compression token scan preserves because it matches AKIA pattern.
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

        # The AWS key line must survive even though it starts with "gpg:"
        # (which the git_log handler strips)
        assert fake_aws_key in compressed, (
            "AWS key was DROPPED from compressed output — credential scan failed!"
        )

    def test_error_lines_preserved(self, tmp_path):
        """Error lines must survive compression."""
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
        if compressed is None:
            pytest.skip("Compression not triggered")

        assert "Error" in compressed or "error" in compressed, (
            "Error line was dropped from compressed output!"
        )


# ============================================================================
# Hook fail-open: any exception → pass through
# ============================================================================

class TestFailOpen:
    def test_malformed_json_input_does_not_crash(self, tmp_path):
        """Hook must not crash on malformed JSON stdin."""
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input="not valid json {{{",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, f"Hook crashed on bad input: {proc.stderr}"

    def test_missing_fields_does_not_crash(self, tmp_path):
        """Hook must not crash when expected fields are missing."""
        payload = json.dumps({"tool_name": "Bash"})
        proc = subprocess.run(
            [sys.executable, str(BASH_COMPRESS_HOOK)],
            input=payload,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 0, f"Hook crashed on partial payload: {proc.stderr}"


# ============================================================================
# Unit C invariant: never show MORE than baseline
# ============================================================================

class TestUnitCInvariant:
    """The compressed result must never be LARGER than the original output."""

    def test_git_log_compressed_not_larger(self, tmp_path):
        """Compressed output <= original size."""
        cmd = "git log --oneline | head -20"
        for length in [500, 2000, 8000, 15000]:
            output = "x" * length  # Boring output, should compress well
            proc = _run_hook(cmd, output)
            assert proc.returncode == 0
            compressed = _get_updated_stdout(proc)
            if compressed is not None:
                assert len(compressed) <= len(output), (
                    f"Compressed ({len(compressed)} chars) > original ({len(output)} chars)!"
                )

    def test_compression_never_inflates_output(self, tmp_path):
        """Even on pathological input, compression should not inflate."""
        cmd = "find src -name '*.py' | xargs wc -l"
        # Create output that's hard to compress (all unique lines)
        output = "\n".join(f"unique line number {i:08d} with some padding text" for i in range(100))

        proc = _run_hook(cmd, output)
        assert proc.returncode == 0
        compressed = _get_updated_stdout(proc)
        if compressed is not None:
            assert len(compressed) <= len(output), (
                f"Compressed output must not exceed original: "
                f"{len(compressed)} > {len(output)}"
            )
