#!/usr/bin/env python3
"""PageRank symbol ranking for skeleton selection ``pagerank_symbols`` ranks a file's symbols by reference-graph centrality so
skeleton truncation keeps the most-referenced symbols instead of the
first-N-by-source-order. Pure stdlib, deterministic, capped.

Run: python3 -m pytest tests/test_structure_map_v2_pagerank.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import structure_map as sm  # noqa: E402


def _graph_with_helper_called_by(n_callers: int) -> sm.ReferenceGraph:
    src = "def helper():\n    return 1\ndef main():\n    return helper()\n"
    src += "".join(f"def f{i}():\n    return helper()\n" for i in range(n_callers))
    return sm.extract_reference_graph(src, "python", "m.py")


# ---------------------------------------------------------------------------
# pagerank_symbols
# ---------------------------------------------------------------------------

def test_helper_called_by_many_ranks_above_main():
    g = _graph_with_helper_called_by(10)
    r = sm.pagerank_symbols(g)
    assert r["helper"] > r["main"], (r["helper"], r["main"])


def test_symbols_with_no_references_tail_in_source_order():
    # `lonely` is never called; `helper` is called by f0. helper must rank
    # above lonely, and lonely keeps source order at the tail.
    src = (
        "def lonely():\n    return 0\n"
        "def helper():\n    return 1\n"
        "def f0():\n    return helper()\n"
    )
    g = sm.extract_reference_graph(src, "python", "m.py")
    r = sm.pagerank_symbols(g)
    assert r["helper"] > r["lonely"]
    # lonely still has a defined (stable) score.
    assert r["lonely"] > 0


def test_pagerank_is_deterministic():
    g = _graph_with_helper_called_by(10)
    assert sm.pagerank_symbols(g) == sm.pagerank_symbols(g)


def test_empty_graph_returns_empty_dict():
    assert sm.pagerank_symbols(sm.ReferenceGraph((), ())) == {}


def test_self_loop_only_does_not_raise():
    g = sm.ReferenceGraph(("a",), (("a", "a"),))
    r = sm.pagerank_symbols(g)
    assert "a" in r and r["a"] > 0


def test_pagerank_respects_node_cap():
    # A graph with >5000 nodes returns a uniform ranking without iterating.
    nodes = tuple(f"n{i}" for i in range(6000))
    g = sm.ReferenceGraph(nodes, ())
    r = sm.pagerank_symbols(g)
    assert len(r) == 6000
    scores = set(round(s, 12) for s in r.values())
    assert len(scores) == 1, "large graph should get a uniform ranking"


def test_pagerank_respects_iteration_cap():
    g = _graph_with_helper_called_by(10)
    # iterations beyond the cap are clamped; result is still stable/defined.
    r_big = sm.pagerank_symbols(g, iterations=10000)
    r_cap = sm.pagerank_symbols(g, iterations=sm._PAGERANK_MAX_ITERATIONS)
    assert r_big == r_cap


# ---------------------------------------------------------------------------
# Skeleton selection keeps highest-centrality symbols
# ---------------------------------------------------------------------------

def test_skeleton_keeps_highest_centrality_symbols_under_limit():
    # 15 functions: helper is called by 10, main by 1, the rest unreferenced.
    # A skeleton with MAX_SKELETON_TOP_LEVEL_FUNCTIONS=8 must keep helper and
    # drop the unreferenced tail.
    src = "def helper():\n    return 1\n"
    src += "def main():\n    return helper()\n"
    src += "".join(f"def f{i}():\n    return helper()\n" for i in range(10))
    src += "".join(f"def unused_{i}():\n    return {i}\n" for i in range(5))
    # Pad so the file is large enough to choose a skeleton and need truncation.
    src += "\n# " + "x" * 4000 + "\n"
    result = sm.summarize_python_source(src, file_path="m.py")
    text = result.replacement_text
    # helper is the highest-centrality symbol; it must survive truncation.
    assert "helper" in text, f"helper should survive PageRank-ordered truncation:\n{text}"
    # The unreferenced tail (unused_4) should be dropped before helper.
    assert "unused_4" not in text, (
        f"low-centrality unused_4 should be dropped before helper:\n{text}"
    )


def test_skeleton_rendered_with_limit_shows_top_centrality():
    # Direct renderer check: with a ranking, _render_skeleton orders functions
    # by centrality, so the first 3 shown are the highest-centrality ones.
    funcs = tuple(
        sm._FunctionSummary(name=name, signature=f"def {name}()", lineno=i, decorators=(), is_async=False)
        for i, name in enumerate(["low1", "low2", "low3", "helper", "low4", "low5", "low6"])
    )
    ranking = {"helper": 0.9, "low1": 0.01, "low2": 0.01, "low3": 0.01,
               "low4": 0.01, "low5": 0.01, "low6": 0.01}
    lines = sm._render_skeleton(
        imports=(), classes=(), functions=funcs, assignments=(), ranking=ranking,
    )
    fn_lines = [ln for ln in lines if "def " in ln]
    # MAX_SKELETON_TOP_LEVEL_FUNCTIONS = 8, so all 7 fit; helper must come first.
    assert "helper" in fn_lines[0], fn_lines
