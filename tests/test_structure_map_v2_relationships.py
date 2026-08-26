#!/usr/bin/env python3
"""reference graph + one-hop relationships + periphery skeleton Covers ``extract_reference_graph``, ``resolve_one_hop_relationships``, and
``build_periphery_skeleton`` in structure_map.py, plus the read_cache wiring
that injects the periphery block as additionalContext. The relationship path
is on-read only and never persists a graph or writes files during resolution.

Run: python3 -m pytest tests/test_structure_map_v2_relationships.py -v
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import structure_map as sm  # noqa: E402


# ---------------------------------------------------------------------------
# extract_reference_graph
# ---------------------------------------------------------------------------

def test_python_reference_graph_collects_call_edges():
    src = (
        "def helper():\n    return 1\n"
        "def main():\n    return helper()\n"
        "def a():\n    return helper()\n"
        "def b():\n    return helper()\n"
    )
    g = sm.extract_reference_graph(src, "python", "m.py")
    assert "helper" in g.nodes
    assert "main" in g.nodes
    callees = {callee for (_, callee) in g.edges}
    assert "helper" in callees
    # helper is referenced by main, a, b (3 incoming edges)
    incoming = [c for (c, t) in g.edges if t == "helper"]
    assert sorted(incoming) == ["a", "b", "main"]


def test_reference_graph_empty_for_unsupported_language():
    g = sm.extract_reference_graph("x = 1", "rust", "m.rs")
    assert g.nodes == () and g.edges == ()


def test_reference_graph_no_raise_on_syntax_error():
    g = sm.extract_reference_graph("def broken(:\n", "python", "m.py")
    assert g.nodes == () and g.edges == ()


# ---------------------------------------------------------------------------
# resolve_one_hop_relationships — Python
# ---------------------------------------------------------------------------

def test_python_resolves_two_sibling_modules_one_hop(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sibling_a.py").write_text("def fa():\n    return 1\n", encoding="utf-8")
    (pkg / "sibling_b.py").write_text("def fb():\n    return 2\n", encoding="utf-8")
    (pkg / "mod.py").write_text(
        "from . import sibling_a\nfrom . import sibling_b\n", encoding="utf-8"
    )
    related = sm.resolve_one_hop_relationships(str(pkg / "mod.py"), "python")
    names = sorted(Path(r).name for r in related)
    assert names == ["sibling_a.py", "sibling_b.py"]
    # One hop only: sibling_a does not import anything, and even if it did,
    # its relations must NOT appear here.
    assert all("mod.py" not in r for r in related)


def test_python_relative_from_sub_resolves_within_package(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sub.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "mod.py").write_text("from .sub import f\n", encoding="utf-8")
    related = sm.resolve_one_hop_relationships(str(pkg / "mod.py"), "python")
    assert any(r.endswith("sub.py") for r in related), related


def test_python_broken_or_nonexistent_import_skipped(tmp_path):
    (tmp_path / "mod.py").write_text(
        "from .missing import x\nimport nonexistent_pkg\n", encoding="utf-8"
    )
    related = sm.resolve_one_hop_relationships(str(tmp_path / "mod.py"), "python")
    assert related == []


def test_python_binary_sibling_skipped(tmp_path):
    (tmp_path / "mod.py").write_text("from . import data\n", encoding="utf-8")
    (tmp_path / "data.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)
    related = sm.resolve_one_hop_relationships(str(tmp_path / "mod.py"), "python")
    assert related == []


def test_python_result_count_respects_n_bound(tmp_path):
    (tmp_path / "mod.py").write_text(
        "".join(f"from . import m{i}\n" for i in range(20)), encoding="utf-8"
    )
    for i in range(20):
        (tmp_path / f"m{i}.py").write_text(f"def f{i}():\n    return {i}\n", encoding="utf-8")
    related = sm.resolve_one_hop_relationships(str(tmp_path / "mod.py"), "python", limit=4)
    assert len(related) == 4


# ---------------------------------------------------------------------------
# resolve_one_hop_relationships — JS/TS
# ---------------------------------------------------------------------------

def test_js_ts_resolves_relative_imports_extension_insensitive(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "util.ts").write_text("export const a = 1;\n", encoding="utf-8")
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "x.tsx").write_text("export const b = 2;\n", encoding="utf-8")
    (src_dir / "main.ts").write_text(
        "import { a } from './util';\nimport { b } from '../lib/x';\n",
        encoding="utf-8",
    )
    related = sm.resolve_one_hop_relationships(str(src_dir / "main.ts"), "typescript")
    names = sorted(Path(r).name for r in related)
    assert "util.ts" in names
    assert "x.tsx" in names  # extension-insensitive: ../lib/x -> x.tsx


def test_js_ts_bare_module_import_skipped(tmp_path):
    (tmp_path / "main.ts").write_text("import React from 'react';\n", encoding="utf-8")
    related = sm.resolve_one_hop_relationships(str(tmp_path / "main.ts"), "typescript")
    assert related == []


# ---------------------------------------------------------------------------
# build_periphery_skeleton
# ---------------------------------------------------------------------------

def test_build_periphery_skeleton_under_budget_with_skeletons(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # A sizable sibling so summarize_code_file yields a real skeleton.
    sib = pkg / "sibling.py"
    body = "\n".join(
        f"def func_{i}(self, v=None):\n    '''doc {i}'''\n    return v or {i}\n" for i in range(40)
    )
    sib.write_text(body, encoding="utf-8")
    (pkg / "mod.py").write_text("from . import sibling\n", encoding="utf-8")
    related = sm.resolve_one_hop_relationships(str(pkg / "mod.py"), "python")
    block = sm.build_periphery_skeleton(related)
    assert block, "expected a non-empty periphery block"
    assert len(block) <= sm.PERIPHERY_BUDGET_CHARS
    assert "periphery: sibling.py" in block


def test_build_periphery_skeleton_empty_when_nothing_fits(tmp_path):
    block = sm.build_periphery_skeleton([])
    assert block == ""


# ---------------------------------------------------------------------------
# No persistence: resolution + build create no files
# ---------------------------------------------------------------------------

def test_relationship_path_writes_no_files(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "sibling.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "mod.py").write_text("from . import sibling\n", encoding="utf-8")
    before = {str(p) for p in tmp_path.rglob("*")}
    related = sm.resolve_one_hop_relationships(str(pkg / "mod.py"), "python")
    block = sm.build_periphery_skeleton(related)
    assert block or block == ""
    after = {str(p) for p in tmp_path.rglob("*")}
    # No new files created by resolution/build.
    assert after == before, f"relationship path created files: {after - before}"


# ---------------------------------------------------------------------------
# read_cache integration: periphery injected as additionalContext
# ---------------------------------------------------------------------------

def test_read_cache_injects_periphery_for_target_with_siblings(tmp_path):
    import json
    import subprocess

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # A sizable sibling so the periphery block is non-empty.
    sib = pkg / "sibling.py"
    body = "\n".join(
        f"def func_{i}(self, v=None):\n    '''worker {i} with body'''\n"
        f"    acc = []\n    for j in range(10):\n        acc.append(j)\n    return acc\n"
        for i in range(60)
    )
    sib.write_text(body, encoding="utf-8")
    # Target: a python file >=16KB that imports the sibling.
    target = pkg / "mod.py"
    target_body = "from . import sibling\n\n" + "\n".join(
        f"def caller_{i}():\n    '''caller body {i} with enough text to push the "
        f"file over the 16KB shadow floor while staying skeleton-friendly'''\n"
        f"    return sibling.func_{i % 60}()\n"
        for i in range(120)
    )
    # Pad to >=18KB.
    if len(target_body.encode("utf-8")) < 18 * 1024:
        target_body += "\n# " + "x" * (18 * 1024 - len(target_body.encode("utf-8")) - 2) + "\n"
    target.write_text(target_body, encoding="utf-8")

    session = "22222222-2222-2222-2222-222222222222"
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_ACTIVE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_SHADOW", "1")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 0, "limit": 0},
        "session_id": session,
        "agent_id": session,
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "read_cache.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout) if out.stdout.strip() else None
    assert parsed is not None, "expected an additionalContext response for periphery"
    hso = parsed.get("hookSpecificOutput", {})
    # Target served full (no deny); periphery injected as additionalContext.
    assert hso.get("permissionDecision") != "deny"
    assert "additionalContext" in hso, "periphery block should be injected"
    assert "periphery" in hso["additionalContext"].lower()


def test_read_cache_drops_periphery_when_over_cap(tmp_path):
    """When the combined block exceeds the strict additional-context cap,
    no additionalContext is emitted (target still served full)."""
    import json
    import subprocess

    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    # Many sizable siblings so the periphery block would exceed the strict cap
    # (1000 chars) when save-hook-additional-context is enabled.
    for i in range(6):
        sib = pkg / f"sib_{i}.py"
        body = "\n".join(
            f"def func_{j}(self, v=None):\n    '''worker {j}'''\n    return v or {j}\n"
            for j in range(40)
        )
        sib.write_text(body, encoding="utf-8")
    target = pkg / "mod.py"
    body = "from . import sib_0, sib_1, sib_2, sib_3, sib_4, sib_5\n\n" + "\n".join(
        f"def caller_{i}():\n    '''caller {i} body text to exceed the 16KB floor'''\n"
        f"    return sib_0.func_0()\n"
        for i in range(120)
    )
    if len(body.encode("utf-8")) < 18 * 1024:
        body += "\n# " + "x" * (18 * 1024 - len(body.encode("utf-8")) - 2) + "\n"
    target.write_text(body, encoding="utf-8")

    session = "33333333-3333-3333-3333-333333333333"
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_ACTIVE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_SHADOW", "1")
    # Strict cap (1000 chars): the multi-file periphery block exceeds it.
    env["CLAUDE_CODE_SAVE_HOOK_ADDITIONAL_CONTEXT"] = "1"
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 0, "limit": 0},
        "session_id": session,
        "agent_id": session,
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "read_cache.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    parsed = json.loads(out.stdout) if out.stdout.strip() else None
    if parsed is not None:
        hso = parsed.get("hookSpecificOutput", {})
        assert hso.get("permissionDecision") != "deny"
        # Over the strict cap -> periphery dropped (target still served full).
        assert "additionalContext" not in hso, (
            "periphery must be dropped when over the strict additional-context cap"
        )
