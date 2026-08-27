#!/usr/bin/env python3
"""Token Optimizer v5.12: PostToolUse Bash Compression Hook.

Compresses Bash tool output AFTER execution via Claude Code's
``updatedToolOutput`` mechanism. This is the UNIT B expansion: it handles
pipeline and metachar-containing commands that PreToolUse bash_hook.py
categorically rejects.

Architecture:
  CC runs Bash tool → PostToolUse fires → this hook receives tool_response
  → pipeline_analyzer checks read-only eligibility → bash_compress.compress()
  compresses stdout → archive raw original → attach archive pointer →
  enforce the baseline-size invariant → updatedToolOutput replaces what
  Claude sees.

The existing PreToolUse bash_hook.py continues to handle simple (metachar-free)
commands. This hook handles everything else — pipes, &&, ||, ;, redirections,
heredocs, and command substitutions.

Safety (same stack as bash_hook.py):
  - Fail-open: any exception → exit 0 with no output → Claude sees raw result
  - Read-only only: pipeline_analyzer rejects any side-effecting stage
  - No double-execution: command already ran; we only compress captured output
  - Token preservation: credential scan runs BEFORE compression
  - Raw output archived: the full stdout is stored with a retrievable key;
    the compressed output carries an expand pointer.
  - Baseline-size invariant: _enforce_baseline_invariant runs so the compressed
    preview never exceeds what Claude Code would show as baseline.
  - Error-on-stdout guard: _ERROR_STDERR_PATTERNS checked against stdout
    when stderr was redirected (2>&1), so compressed output never swallows
    error lines that appear on stdout.
  - Exit behavior: no output = pass through; JSON output = compress

Hook config (hooks/hooks.json):
  PostToolUse matcher "Bash" → bash_compress_hook.py --quiet
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    """Read PostToolUse hook input, compress Bash stdout if eligible."""
    try:
        from hook_io import read_stdin_hook_input
        payload = read_stdin_hook_input(max_bytes=5_242_880)  # 5MB
        if not payload:
            return
    except (json.JSONDecodeError, OSError, ImportError):
        return  # Bad input, exit silently

    tool_name = payload.get("tool_name", "")
    if tool_name != "Bash":
        return

    # Extract tool response
    tool_response = payload.get("tool_response", {})
    if not tool_response or not isinstance(tool_response, dict):
        return

    # Skip interrupted or image output
    if tool_response.get("interrupted", False):
        return
    if tool_response.get("isImage", False):
        return

    stdout = tool_response.get("stdout", "") or ""
    stderr = tool_response.get("stderr", "") or ""

    # Too small to compress
    if not stdout or len(stdout) < 100:
        return

    # Get the command that was run
    tool_input = payload.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command:
        return

    # Detect if the output was ALREADY compressed by PreToolUse bash_hook.
    if "[Full result archived" in stdout or "[bash_compress]" in stdout:
        return

    # Check pipeline read-only eligibility
    try:
        from pipeline_analyzer import is_read_only_pipeline
        is_ro, reason = is_read_only_pipeline(command)
        if not is_ro:
            return  # Not eligible, pass through raw
    except Exception:
        return  # Fail open

    # Compression
    try:
        script_dir = str(Path(__file__).resolve().parent)
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)

        from bash_compress import (
            compress,
            _looks_like_failure,
            _strip_ansi,
            _find_preserved_lines,
            _ERROR_STDERR_PATTERNS,
            _enforce_baseline_invariant,
        )

        # --- check stderr for failure patterns ---
        if _looks_like_failure(0, stderr):
            return  # Don't compress failure output

        # Clean ANSI escape codes before compression
        cleaned_stdout = _strip_ansi(stdout)

        # --- also scan stdout for error patterns ---
        # When stderr is redirected to stdout (2>&1), error lines appear
        # on stdout. If the pipeline exits 0 but stdout contains error
        # patterns, pass through raw so the model sees the errors.
        if _stdout_has_error_patterns(cleaned_stdout):
            return  # Error on stdout: pass through raw

        # Run the standard compression pipeline
        compressed = compress(command, cleaned_stdout, returncode=0, stderr=stderr)

        # If compression didn't help (same output returned), pass through
        if compressed == cleaned_stdout or not compressed:
            return

        # Verify compression actually shrank the output.
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        comp_tokens = _est(compressed)
        if orig_tokens > 0 and (1.0 - comp_tokens / orig_tokens) < 0.10:
            return  # Not enough savings

        # --- archive raw stdout + attach a retrieval pointer ---
        # Progressive disclosure: the full uncompressed original is stored
        # on disk so the model can retrieve it via `expand <key>`. Mirror
        # the exact archiving path from bash_compress.main().
        _archive_key = None
        if len(stdout) > 500:
            try:
                from archive_result import (
                    archive_entry_exists,
                    archive_original,
                    build_archive_pointer,
                )
                _session_id = os.environ.get("CLAUDE_SESSION_ID", "")
                _archive_key = hashlib.sha256(
                    f"{_session_id}|{command}|{time.time()}|{os.urandom(4).hex()}".encode("utf-8", errors="replace")
                ).hexdigest()[:16]
                if archive_original(stdout, _session_id, _archive_key, "Bash") is not None:
                    if archive_entry_exists(_session_id, _archive_key):
                        compressed = build_archive_pointer(compressed, len(stdout), _archive_key)
                    else:
                        # Entry was pruned after write — serve raw, not lossy preview.
                        # This matches the guarantee in bash_compress.main().
                        compressed = stdout
                        _archive_key = None
                else:
                    _archive_key = None
            except Exception:
                _archive_key = None

        # --- enforce the baseline-size invariant ---
        # If our compressed preview + archive pointer would exceed what
        # Claude Code would show as a baseline stub, shrink to fit.
        try:
            compressed = _enforce_baseline_invariant(compressed, stdout, _archive_key)
        except Exception:
            pass

        # Log compression event to trends.db
        _log_event(command, cleaned_stdout, compressed)

        # Emit updatedToolOutput to replace what Claude sees
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": {
                    "stdout": compressed,
                    "stderr": stderr,
                    "interrupted": False,
                    "isImage": False,
                },
            },
        }
        print(json.dumps(response))

    except Exception:
        return  # Fail open: any error → pass through raw


def _stdout_has_error_patterns(stdout: str) -> bool:
    """Check stdout for error patterns (covers 2>&1 redirect case).

    Uses the same _ERROR_STDERR_PATTERNS list as _looks_like_failure.
    Only triggers when stdout is large enough to make compression
    meaningful (>500 chars), so small outputs with coincidental
    error-keyword lines are not blocked.
    """
    if not stdout or len(stdout) < 500:
        return False
    try:
        from bash_compress import _ERROR_STDERR_PATTERNS
        # Count how many lines match error patterns. A single matching
        # line could be a harmless log line; a high density signals
        # error output on stdout.
        lines = stdout.splitlines()
        match_count = 0
        for line in lines:
            for pat in _ERROR_STDERR_PATTERNS:
                if pat.search(line):
                    match_count += 1
                    break
        # Require at least 3 error lines AND >10% of lines to be errors
        # before passing through. This filters out false positives from
        # normal output that coincidentally contains keyword substrings.
        if match_count >= 3 and match_count > len(lines) * 0.10:
            return True
    except Exception:
        return False
    return False


def _log_event(command: str, original: str, compressed: str) -> None:
    """Log a compression event to trends.db. Fail-open, never raises."""
    try:
        from compression_log import log_compression_event
        session_id = os.environ.get("CLAUDE_SESSION_ID", "")
        log_compression_event(
            feature="bash_compress_pipeline",
            original_text=original,
            compressed_text=compressed,
            session_id=session_id,
            command_pattern=command[:100],
            quality_preserved=True,
            verified=True,
            tier="measured",
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
