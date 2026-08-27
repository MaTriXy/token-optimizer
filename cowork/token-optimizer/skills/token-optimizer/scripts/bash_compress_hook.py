#!/usr/bin/env python3
"""Token Optimizer v5.12: PostToolUse Bash Compression Hook.

Compresses Bash tool output AFTER execution via Claude Code's
``updatedToolOutput`` mechanism. This is the UNIT B expansion: it handles
pipeline and metachar-containing commands that PreToolUse bash_hook.py
categorically rejects.

Architecture:
  CC runs Bash tool → PostToolUse fires → this hook receives tool_response
  → pipeline_analyzer checks read-only eligibility → bash_compress.compress()
  compresses stdout → updatedToolOutput replaces what Claude sees.

The existing PreToolUse bash_hook.py continues to handle simple (metachar-free)
commands. This hook handles everything else — pipes, &&, ||, ;, redirections,
heredocs, and command substitutions.

Safety (same stack as bash_hook.py):
  - Fail-open: any exception → exit 0 with no output → Claude sees raw result
  - Read-only only: pipeline_analyzer rejects any side-effecting stage
  - No double-execution: command already ran; we only compress captured output
  - Token preservation: credential scan runs BEFORE compression
  - Raw output archived: existing archive_result.py PostToolUse hooks run
    alongside this one (separate matcher group)
  - Exit behavior: no output = pass through; JSON output = compress

Hook config (hooks/hooks.json):
  PostToolUse matcher "Bash" → bash_compress_hook.py --quiet
"""

from __future__ import annotations

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
    # When bash_hook rewrites a command to run through bash_compress.py,
    # the compressed output carries an archive pointer suffix like
    # "[Full result archived (N chars)...". If present, skip double-compression.
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

        from bash_compress import compress, _looks_like_failure, _strip_ansi, _find_preserved_lines

        # Check stderr for failure patterns (commands can exit 0 with errors on stderr)
        if _looks_like_failure(0, stderr):
            return  # Don't compress failure output

        # Clean ANSI escape codes before compression
        cleaned_stdout = _strip_ansi(stdout)

        # Run the standard compression pipeline
        # Note: command_str is the full pipeline command (e.g. "git log | head").
        # bash_compress._detect_pattern() looks at the first stage,
        # which works for most pipeline shapes (git log | head → git_log handler).
        # For unrecognized patterns, the generic structural compressor kicks in.
        compressed = compress(command, cleaned_stdout, returncode=0, stderr=stderr)

        # If compression didn't help (same output returned), pass through
        if compressed == cleaned_stdout or not compressed:
            return

        # Verify compression actually shrank the output. If not, don't replace.
        # The 10% gate inside compress() already handles this, but double-check.
        from token_estimate import estimate_tokens as _est
        orig_tokens = _est(cleaned_stdout)
        comp_tokens = _est(compressed)
        if orig_tokens > 0 and (1.0 - comp_tokens / orig_tokens) < 0.10:
            return  # Not enough savings

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
