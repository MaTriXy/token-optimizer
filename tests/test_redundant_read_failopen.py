#!/usr/bin/env python3
"""the redundant-read path fails open (never crashes the hook).

A deeply-nested AST can raise RecursionError inside the reference-graph walk
that orders skeleton truncation. That path runs inside the PreToolUse Read hook
(_summarize_redundant_read -> summarize_code_source -> ... -> pagerank), so an
uncaught error would crash the hook and could block the user's Read. These tests
prove every layer degrades to "serve normally" instead of raising.

Run: python3 -m pytest tests/test_redundant_read_failopen.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import read_cache  # noqa: E402
import structure_map as sm  # noqa: E402


def _deep_call_source(depth: int) -> str:
    """A parseable file whose reference-graph walk recurses `depth` deep."""
    body = "def f(x):\n    return x\n\n"
    body += "result = " + "f(" * depth + "1" + ")" * depth + "\n"
    # padding so the file clears MIN_TOKENS_FOR_STRUCTURE with unique bodies
    body += "\n".join(
        f"def util_{i}(a, b):\n    return a * {i} + b - {i}" for i in range(90)
    ) + "\n"
    return body


# ---------------------------------------------------------------------------
# The reference-graph builder itself degrades to an empty graph, never raises.
# ---------------------------------------------------------------------------

def test_reference_graph_deep_ast_returns_empty_not_raise():
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(800)
    try:
        src = _deep_call_source(1500)
        graph = sm._extract_python_reference_graph(src, "deep.py")
    finally:
        sys.setrecursionlimit(old)
    # Empty (degraded) is acceptable; the contract is "no exception".
    assert graph is not None
    assert len(graph.nodes) == 0 or len(graph.nodes) >= 0


def test_summarize_code_source_deep_ast_no_crash():
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(800)
    try:
        src = _deep_call_source(1500)
        result = sm.summarize_code_source(src, file_path="deep.py")
    finally:
        sys.setrecursionlimit(old)
    # Whatever tier it lands on, it must return a result rather than raising.
    assert result is not None
    assert result.file_path == "deep.py"


def test_ranking_failure_still_serves(monkeypatch):
    """If pagerank/graph raises unexpectedly, summarize still returns a valid
    (eligible) skeleton -- the ranking is only an ordering hint."""
    def boom(*_a, **_k):
        raise RecursionError("boom")
    monkeypatch.setattr(sm, "pagerank_symbols", boom)
    src = "\n".join(
        f"def handler_{i}(req, ctx=None):\n"
        f"    '''Handle {i} mode {i % 5}.'''\n"
        f"    if req is None:\n"
        f"        raise ValueError('h{i} {i * 3}')\n"
        f"    total = req.get('p{i}', {i}) * {i + 2}\n"
        f"    return handler_{(i + 1) % 80}(req, ctx) if total < 0 else total"
        for i in range(80)
    ) + "\n"
    result = sm.summarize_code_source(src, file_path="real.py")
    assert result.eligible is True
    assert result.reason == "ok"


# ---------------------------------------------------------------------------
# _summarize_redundant_read (the hook entry point) fails open.
# ---------------------------------------------------------------------------

def test_summarize_redundant_read_catches_summarizer_error(tmp_path, monkeypatch):
    target = tmp_path / "x.py"
    target.write_text("def a():\n    return 1\n", encoding="utf-8")

    def boom(*_a, **_k):
        raise RecursionError("boom from summarizer")

    monkeypatch.setattr(read_cache, "summarize_code_source", boom)
    summary, reason = read_cache._summarize_redundant_read(
        str(target), offset=0, limit=0, file_tokens_est=1000,
    )
    assert summary is None
    assert reason == "summarize_error"


def test_summarize_redundant_read_deep_ast_no_crash(tmp_path):
    target = tmp_path / "deep.py"
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(800)
    try:
        target.write_text(_deep_call_source(1500), encoding="utf-8")
        summary, reason = read_cache._summarize_redundant_read(
            str(target), offset=0, limit=0, file_tokens_est=5000,
        )
    finally:
        sys.setrecursionlimit(old)
    # Returned a tuple, did not raise. Reason is whatever tier it degraded to.
    assert reason is not None
