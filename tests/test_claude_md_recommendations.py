"""Tests for the CLAUDE.md slim recommendation guards and savings math.

Pins the behavior introduced in the citation-fix PR:
- The "Slim CLAUDE.md" quick-win only fires when the file is actually over
  200 lines (Anthropic's documented guidance), not just when it's token-dense.
- The "tokens recoverable" / "savings" figure is computed against the 200-line
  target using measured tokens-per-line, not the old ~4,500-token heuristic.
- The medium tier fires only when lines > 200 and tokens > 5000.

Covers both generate_auto_recommendations() and quick_scan().

Run: python3 -m pytest tests/test_claude_md_recommendations.py -v
"""
import importlib
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture
def measure(monkeypatch):
    """Import measure.py fresh under a temp snapshot dir (for _auto_snapshot)."""
    tmp = tempfile.mkdtemp(prefix="to-claude-md-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _claude_md_components(lines, tokens):
    """Build a minimal components dict with a single CLAUDE.md file."""
    return {
        "claude_md_home": {
            "path": "/fake/CLAUDE.md",
            "exists": True,
            "tokens": tokens,
            "lines": lines,
        }
    }


# --- generate_auto_recommendations: guard + math ---------------------------

def test_no_slim_when_under_200_lines_even_if_token_dense(measure):
    """A 150-line / 7000-token file must NOT trigger any slim recommendation.
    The old code fired on tokens > 6000 alone; the guard on lines > 200 prevents
    a self-contradicting 'slim to 200 lines' advice for a file already under 200."""
    components = _claude_md_components(lines=150, tokens=7000)
    plan, _ = measure.generate_auto_recommendations(components)
    assert "**Slim " not in plan
    assert "Consider slimming " not in plan


def test_quick_tier_recoverable_math(measure):
    """A 400-line / 8000-token file: recoverable == 8000 - int(200 * 8000/400) == 4000.
    The old heuristic would have reported 8000 - 4500 = 3500; the new math reports 4000."""
    components = _claude_md_components(lines=400, tokens=8000)
    plan, _ = measure.generate_auto_recommendations(components)
    assert "**Slim /fake/CLAUDE.md" in plan
    # New math: 8000 - int(200 * 20.0) = 8000 - 4000 = 4000.
    assert "~4,000 tokens recoverable" in plan
    # Old heuristic figure must not appear.
    assert "3,500 tokens recoverable" not in plan


def test_medium_tier_fires_for_over_200_lines_and_over_5000_tokens(measure):
    """A 250-line / 5500-token file: not quick tier (tokens <= 6000), but medium tier
    fires (lines > 200 and tokens > 5000). Reports both line count and token count."""
    components = _claude_md_components(lines=250, tokens=5500)
    plan, _ = measure.generate_auto_recommendations(components)
    assert "Consider slimming /fake/CLAUDE.md" in plan
    assert "**Slim " not in plan  # quick tier must not fire
    assert "250 lines" in plan
    assert "5,500 tokens" in plan


def test_medium_tier_does_not_fire_when_under_200_lines(measure):
    """A 150-line / 5500-token file: neither tier fires (lines <= 200)."""
    components = _claude_md_components(lines=150, tokens=5500)
    plan, _ = measure.generate_auto_recommendations(components)
    assert "**Slim " not in plan
    assert "Consider slimming " not in plan


# --- quick_scan: guard + savings math ---------------------------------------

def _quick_scan_with_claude_md(measure, monkeypatch, lines, tokens):
    """Run quick_scan(as_json=True) with a synthetic CLAUDE.md component.

    Monkeypatches measure_components so no filesystem CLAUDE.md is read, and
    _collect_trends_data so no trends db / JSONL scan runs.
    """
    components = _claude_md_components(lines=lines, tokens=tokens)
    monkeypatch.setattr(measure, "measure_components", lambda: components)
    monkeypatch.setattr(measure, "_collect_trends_data", lambda days=30: None)
    return measure.quick_scan(as_json=True)


def test_quick_scan_no_slim_when_under_200_lines(measure, monkeypatch):
    """quick_scan must not emit a slim quick-win for a token-dense but short file
    (150 lines, 7000 tokens). The old code fired on tokens > 5000 alone."""
    result = _quick_scan_with_claude_md(measure, monkeypatch, lines=150, tokens=7000)
    assert result["quick_win"] is None


def test_quick_scan_slim_savings_math(measure, monkeypatch):
    """quick_scan savings == tokens - int(200 * tokens/lines) for an over-200-line file.
    For 400 lines / 8000 tokens: savings == 8000 - 4000 == 4000."""
    result = _quick_scan_with_claude_md(measure, monkeypatch, lines=400, tokens=8000)
    assert result["quick_win"] is not None
    expected_savings = 8000 - int(200 * (8000 / 400))
    assert result["quick_win"]["savings"] == expected_savings  # == 4000
    assert "under 200" in result["quick_win"]["action"]


def test_quick_scan_token_dense_short_file_does_not_mask_a_valid_slim_win(measure, monkeypatch):
    """G3 C-P2-1 regression: a token-dense but SHORT file (largest by tokens,
    <=200 lines) must not suppress a genuine slim win in a different over-200-line
    file. Old code did max()-by-tokens then gated on lines>200, so the short file
    won and failed the gate -> quick_win None. The fix filters to qualifying files
    first, so the 400-line file's win fires and names THAT file."""
    components = {
        "claude_md_home": {
            "path": "/home/u/CLAUDE.md",
            "exists": True, "tokens": 9000, "lines": 180,  # largest by tokens, short
        },
        "claude_md_project_proj": {
            "path": "/home/u/proj/CLAUDE.md",
            "exists": True, "tokens": 6000, "lines": 400,  # real slim target
        },
    }
    monkeypatch.setattr(measure, "measure_components", lambda: components)
    monkeypatch.setattr(measure, "_collect_trends_data", lambda days=30: None)
    result = measure.quick_scan(as_json=True)
    assert result["quick_win"] is not None, "valid slim win was masked by the token-dense short file"
    # Names the over-200-line file, not the short one.
    assert "/home/u/proj/CLAUDE.md" in result["quick_win"]["action"]
    assert "400 lines" in result["quick_win"]["action"]
    assert result["quick_win"]["savings"] == 6000 - int(200 * (6000 / 400))  # == 3000
