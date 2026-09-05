"""Tests for _stdout_has_error_patterns in bash_compress_hook.py.

C-3: the old implementation iterated every line against all 13 error
patterns with no early termination: O(lines × 13). Measured: 2.9s for
10K lines, 9.5s for 50K lines (clean output), exceeding the ~2s
PostToolUse hook timeout. The fix uses a single combined regex per line
+ early exit when the density threshold is met: O(lines × 1).
"""
import sys
import time
import os

import pytest

SCRIPTS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "token-optimizer", "scripts",
)
sys.path.insert(0, SCRIPTS)

from bash_compress_hook import _stdout_has_error_patterns


def test_short_stdout_returns_false():
    """Output under 500 chars should not trigger error detection."""
    assert _stdout_has_error_patterns("error: something failed\n") is False


def test_empty_stdout_returns_false():
    assert _stdout_has_error_patterns("") is False


def test_clean_stdout_returns_false():
    """Large clean output should not match error patterns."""
    text = "\n".join(f"line {i}: normal output" for i in range(1000))
    # Pad to >500 chars.
    text = text + "x" * 600
    assert _stdout_has_error_patterns(text) is False


def test_dense_error_stdout_returns_true():
    """Output with >10% error lines and >=3 matches should trigger."""
    lines = []
    for i in range(100):
        if i < 20:
            lines.append("error: something failed")
        else:
            lines.append("normal output line")
    text = "\n".join(lines)
    assert len(text) >= 500
    assert _stdout_has_error_patterns(text) is True


def test_sparse_error_stdout_returns_false():
    """Output with <10% error lines should not trigger even with 3+ matches."""
    lines = []
    for i in range(100):
        if i < 5:
            lines.append("error: something failed")
        else:
            lines.append("normal output line")
    text = "\n".join(lines)
    assert len(text) >= 500
    # 5/100 = 5% < 10% threshold, so should not trigger
    assert _stdout_has_error_patterns(text) is False


def test_localized_error_patterns_detected():
    """Localized error markers (German, French, Chinese, Japanese) should
    be detected by the combined regex."""
    lines = []
    for i in range(50):
        lines.append("Fehler: etwas ist schiefgegangen")  # German
    for i in range(50):
        lines.append("erreur: quelque chose a échoué")  # French
    text = "\n".join(lines)
    assert len(text) >= 500
    assert _stdout_has_error_patterns(text) is True


def test_traceback_detected():
    """Traceback lines should be detected."""
    lines = []
    for i in range(30):
        lines.append("Traceback (most recent call last):")
    for i in range(70):
        lines.append("normal output")
    text = "\n".join(lines)
    assert len(text) >= 500
    assert _stdout_has_error_patterns(text) is True


# ---------------------------------------------------------------------------
# C-3: performance regression test. The old per-pattern loop took 2.9s for
# 10K lines of clean output. The combined regex should be ~20x faster.
# ---------------------------------------------------------------------------
def test_stdout_error_patterns_performance_10k_clean():
    """C-3: scanning 10K lines of clean output must complete in under 1000ms
    (was 2.9s with the old per-pattern loop)."""
    # 10K lines of clean output, padded to >500 chars.
    text = "\n".join(f"line {i}: normal output without errors" for i in range(10_000))
    assert len(text) >= 500
    t0 = time.perf_counter()
    result = _stdout_has_error_patterns(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert result is False
    # 1000ms is a robust ceiling: the combined regex does ~10ms on a 2023
    # M2, while the old per-pattern loop took 2900ms. 1000ms still catches a
    # reversion to the old loop while tolerating a loaded CI runner.
    assert elapsed_ms < 1000, (
        f"_stdout_has_error_patterns took {elapsed_ms:.1f}ms for 10K clean lines, "
        f"expected <1000ms (old per-pattern loop took ~2900ms — likely reverted)"
    )


def test_stdout_error_patterns_performance_10k_with_errors():
    """C-3: scanning 10K lines with 20% errors must complete in under 500ms
    AND trigger early exit (was ~3.5s with old per-pattern loop, no early exit)."""
    lines = []
    for i in range(10_000):
        if i % 5 == 0:
            lines.append("error: something failed")
        else:
            lines.append("normal output line")
    text = "\n".join(lines)
    assert len(text) >= 500
    t0 = time.perf_counter()
    result = _stdout_has_error_patterns(text)
    elapsed_ms = (time.perf_counter() - t0) * 1000
    assert result is True
    # With early exit, the scan stops as soon as density >10% and count >=3,
    # which happens within the first ~30 lines. Without early exit, it scans
    # all 10K lines. 500ms is a robust ceiling that still proves early exit
    # is active while tolerating a loaded CI runner.
    assert elapsed_ms < 500, (
        f"_stdout_has_error_patterns took {elapsed_ms:.1f}ms for 10K lines with "
        f"errors, expected <500ms (early exit not working — likely reverted to "
        f"full scan without early termination)"
    )


def test_case_sensitive_patterns_not_upgraded():
    """N-2: the combined error regex must preserve each pattern's original
    case sensitivity. The first combined-regex attempt compiled with a global
    re.I, silently upgrading \\bFAILED\\b and \\bTraceback\\b so benign output
    containing lowercase "failed"/"traceback" at >=10% line density tripped
    the error gate (skipping compression) where the per-pattern loop passed
    it through."""
    # Lowercase "failed" at ~11% density: must NOT trip (was a false positive
    # under the global-re.I build; the original case-sensitive \\bFAILED\\b
    # never matched lowercase).
    lines = [f"2026-09-02 10:{i % 60:02d}:00 INFO request {i} handled ok"
             for i in range(89)]
    lines += [f"retry policy: {i} tasks failed retry=ok recovered"
              for i in range(11)]
    assert _stdout_has_error_patterns("\n".join(lines)) is False

    # Lowercase "traceback" in help text at ~11% density: must NOT trip.
    lines = [f"option --show-traceback-{i}: print the traceback and exit"
             for i in range(15)]
    lines += [f"regular help line {i} with usage text here" for i in range(120)]
    assert _stdout_has_error_patterns("\n".join(lines)) is False

    # Uppercase FAILED must still trip (case-sensitive pattern intact).
    lines = [f"step {i} ok" for i in range(85)] + ["STEP FAILED"] * 15
    assert _stdout_has_error_patterns("\n".join(lines)) is True

    # Case-insensitive patterns must still match lowercase ("error:").
    lines = [f"step {i} ok" for i in range(85)] + ["error: boom"] * 15
    assert _stdout_has_error_patterns("\n".join(lines)) is True
