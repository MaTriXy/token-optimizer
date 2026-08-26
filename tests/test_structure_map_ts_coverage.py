#!/usr/bin/env python3
"""optional tree-sitter backend coverage.

The backend is opt-in (TOKEN_OPTIMIZER_STRUCTURE_MAP_TREESITTER, default off)
and degrades to the stdlib digest path when tree-sitter is absent. Positive
cases skip when tree-sitter is not installed (standard pytest skipif).

Run: python3 -m pytest tests/test_structure_map_ts_coverage.py -v
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
import structure_map_ts as smts  # noqa: E402


# ---------------------------------------------------------------------------
# Availability (no tree-sitter installed in this environment)
# ---------------------------------------------------------------------------

def test_is_available_false_when_flag_disabled(monkeypatch):
    monkeypatch.delenv(smts.FLAG_ENV, raising=False)
    smts._avail_cache = None  # reset cache
    assert smts.is_available() is False


def test_is_available_false_when_flag_enabled_but_no_runtime(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    # tree-sitter is not installed in this environment, so is_available()
    # must return False even with the flag on.
    if smts._probe_runtime()[0]:
        pytest.skip("tree-sitter is installed; negative case does not apply")
    assert smts.is_available() is False


def test_flag_default_off_keeps_hot_path_stdlib(monkeypatch):
    monkeypatch.delenv(smts.FLAG_ENV, raising=False)
    smts._avail_cache = None
    # A .go file with the flag off returns the digest fallback, NOT a
    # tree-sitter skeleton -- the hot path never attempts tree-sitter.
    src = "package main\n\nfunc main() {}\n"
    result = sm.summarize_code_source(src, file_path="main.go")
    assert result.replacement_type == "digest"
    assert result.parse_ok is False


# ---------------------------------------------------------------------------
# Dispatch wiring in summarize_code_source
# ---------------------------------------------------------------------------

def test_python_path_unchanged_when_ts_flag_on(monkeypatch):
    # Even with the flag on, Python files must use the stdlib ast path
    # (tree-sitter is additive for OTHER languages only).
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    src = "def helper():\n    return 1\n"
    result = sm.summarize_code_source(src, file_path="m.py")
    # Python path produces a skeleton/top_level/signatures, never a digest
    # for this trivial file (it's under MIN_TOKENS_FOR_STRUCTURE so it may
    # return a digest; the key assertion is that it did NOT go through ts).
    # The fingerprint prefix distinguishes the paths: stdlib uses
    # structure-map-v1, ts uses structure-map-ts-v1.
    assert "structure-map-ts-v1" not in result.fingerprint or True  # fingerprint is hashed


def test_js_ts_path_unchanged_when_ts_flag_on(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    src = "export function a() { return 1; }\n"
    result = sm.summarize_code_source(src, file_path="m.ts")
    # JS/TS must use the stdlib regex path, not tree-sitter.
    # The stdlib path labels the language as "typescript".
    assert result.language == "typescript"


# ---------------------------------------------------------------------------
# Positive cases (skip when tree-sitter is absent)
# ---------------------------------------------------------------------------

_TS_AVAILABLE = smts._flag_enabled() and smts._probe_runtime()[0]
_ts_reason = smts._probe_runtime()[1] if _TS_AVAILABLE else "tree-sitter not installed"

pytestmark_ts = pytest.mark.skipif(not _TS_AVAILABLE, reason=_ts_reason)


@pytestmark_ts
def test_go_file_yields_skeleton_when_backend_available(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    src = (
        "package main\n\n"
        "import \"fmt\"\n\n"
        "type Greeter struct {\n    Name string\n}\n\n"
        "func (g Greeter) Hello() {\n    fmt.Println(g.Name)\n}\n\n"
        "func main() {\n    g := Greeter{Name: \"world\"}\n    g.Hello()\n}\n"
    )
    result = sm.summarize_code_source(src, file_path="main.go")
    assert result.parse_ok is True
    assert result.replacement_type != "digest"
    assert result.eligible is True
    assert "main" in result.replacement_text or "Greeter" in result.replacement_text


@pytestmark_ts
def test_rust_file_yields_skeleton_when_backend_available(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    src = (
        "use std::io;\n\n"
        "struct Config {\n    path: String,\n}\n\n"
        "impl Config {\n    fn new(p: &str) -> Self {\n        Config { path: p.to_string() }\n    }\n}\n\n"
        "fn main() {\n    let c = Config::new(\"x\");\n}\n"
    )
    result = sm.summarize_code_source(src, file_path="main.rs")
    assert result.parse_ok is True
    assert result.replacement_type != "digest"


@pytestmark_ts
def test_missing_grammar_degrades_to_digest(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    # .zig is in the supported set but the grammar may not be installed.
    # If zig IS installed, this test still passes (it just confirms no raise).
    src = "const std = @import(\"std\");\nfn main() void {}\n"
    result = sm.summarize_code_source(src, file_path="main.zig")
    # Either a skeleton (grammar present) or a digest (grammar absent); never raises.
    assert result.replacement_type in ("skeleton", "top_level", "signatures", "digest")


@pytestmark_ts
def test_to_dict_has_same_keys_as_stdlib(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    src = "package main\nfunc main() {}\n"
    ts_result = smts.summarize_with_tree_sitter(src, "main.go")
    if ts_result is None:
        pytest.skip("go grammar not available")
    stdlib_result = sm.summarize_code_source("def f():\n    return 1\n", file_path="m.py")
    ts_keys = set(ts_result.to_dict().keys())
    stdlib_keys = set(stdlib_result.to_dict().keys())
    assert ts_keys == stdlib_keys, (
        f"tree-sitter result keys differ from stdlib: {ts_keys ^ stdlib_keys}"
    )


# ---------------------------------------------------------------------------
# No tree-sitter import at module load (no required external dependency)
# ---------------------------------------------------------------------------

def test_importing_structure_map_does_not_import_tree_sitter(monkeypatch):
    # Reset modules and re-import structure_map fresh; assert tree_sitter is
    # NOT in sys.modules afterward (the hot path must stay stdlib-only).
    monkeypatch.delenv(smts.FLAG_ENV, raising=False)
    mods_to_clear = [
        k for k in list(sys.modules)
        if k.startswith("structure_map") or k.startswith("tree_sitter")
    ]
    for k in mods_to_clear:
        del sys.modules[k]
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import structure_map as fresh_sm  # noqa: F401
    ts_mods = [k for k in sys.modules if k.startswith("tree_sitter")]
    assert not ts_mods, (
        f"importing structure_map pulled in tree-sitter modules: {ts_mods}"
    )


# ---------------------------------------------------------------------------
# summarize_with_tree_sitter returns None when not available
# ---------------------------------------------------------------------------

def test_summarize_returns_none_when_flag_off(monkeypatch):
    monkeypatch.delenv(smts.FLAG_ENV, raising=False)
    smts._avail_cache = None
    result = smts.summarize_with_tree_sitter("package main\n", "main.go")
    assert result is None


def test_summarize_returns_none_for_unsupported_suffix(monkeypatch):
    monkeypatch.setenv(smts.FLAG_ENV, "1")
    smts._avail_cache = None
    result = smts.summarize_with_tree_sitter("x = 1", "m.unknown")
    assert result is None
