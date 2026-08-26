#!/usr/bin/env python3
"""tree-sitter method + namespace extraction, on-read wiring.

fix-5a: methods of brace-body languages (Java/C#/C++/Kotlin/Swift) live under an
intermediate body node (class_body/declaration_list/field_declaration_list) as
GRANDCHILDREN of the class node. The extractor must descend one body level.

fix-5b: namespace_declaration was in BOTH the import set and the class set with
imports checked first, so C#/C++ namespaces (and every class inside them) were
dropped. Namespaces must be descended THROUGH.

fix-6: is_structure_supported_file must include the tree-sitter suffixes when
(and only when) the backend is flag-enabled and available, so the on-read path
actually reaches multi-language structure.

These use fake tree-sitter nodes (a tiny duck-typed tree) so they run without
the tree_sitter package installed -- they exercise the pure-Python walker.

Run: python3 -m pytest tests/test_structure_map_ts_methods.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import importlib  # noqa: E402

import structure_map as sm  # noqa: E402
import structure_map_ts as smts  # noqa: E402


def _live():
    """Return the CURRENTLY-registered structure_map / structure_map_ts modules.

    Another test in the suite deletes + re-imports these modules to prove the
    hot path stays stdlib-only, which swaps the sys.modules objects. The fix-6
    wiring re-imports structure_map_ts by name at call time, so tests that patch
    ``is_available`` must patch the live module object, not a stale binding.
    """
    return importlib.import_module("structure_map"), importlib.import_module("structure_map_ts")


class FakeNode:
    """Minimal duck type of a tree-sitter node used by structure_map_ts."""

    def __init__(self, type_, start_byte=0, end_byte=0, start_row=0, children=None):
        self.type = type_
        self.start_byte = start_byte
        self.end_byte = end_byte
        self.start_point = (start_row, 0)
        self.children = children or []


class _Tree:
    """Builds a name buffer so identifier nodes slice real text from bytes."""

    def __init__(self):
        self.buf = bytearray()

    def ident(self, name):
        s = len(self.buf)
        self.buf.extend(name.encode("utf-8"))
        return FakeNode("identifier", s, len(self.buf))

    @staticmethod
    def n(type_, children=None):
        return FakeNode(type_, 0, 0, 0, children or [])


# ---------------------------------------------------------------------------
# fix-5a: methods are grandchildren under a class body node (Java-style)
# ---------------------------------------------------------------------------

def test_java_class_methods_under_class_body_extracted():
    t = _Tree()
    root = t.n("program", [
        t.n("class_declaration", [
            t.ident("Foo"),
            t.n("class_body", [
                t.n("method_declaration", [t.ident("bar")]),
                t.n("method_declaration", [t.ident("baz")]),
            ]),
        ]),
    ])
    imports, classes, functions = smts._walk_tree(root, bytes(t.buf))
    assert len(classes) == 1
    assert classes[0]["name"] == "Foo"
    method_names = {m["name"] for m in classes[0]["methods"]}
    assert method_names == {"bar", "baz"}, method_names


def test_cpp_struct_methods_under_field_declaration_list():
    t = _Tree()
    root = t.n("translation_unit", [
        t.n("struct_specifier", [
            t.ident("Vec"),
            t.n("field_declaration_list", [
                t.n("function_definition", [t.ident("norm")]),
            ]),
        ]),
    ])
    _, classes, _ = smts._walk_tree(root, bytes(t.buf))
    assert len(classes) == 1
    assert {m["name"] for m in classes[0]["methods"]} == {"norm"}


# ---------------------------------------------------------------------------
# fix-5b: C# namespace is descended through, its classes survive
# ---------------------------------------------------------------------------

def test_csharp_namespace_not_dropped_classes_survive():
    t = _Tree()
    root = t.n("compilation_unit", [
        t.n("namespace_declaration", [
            t.ident("MyNs"),
            t.n("declaration_list", [
                t.n("class_declaration", [
                    t.ident("Widget"),
                    t.n("declaration_list", [
                        t.n("method_declaration", [t.ident("Run")]),
                    ]),
                ]),
            ]),
        ]),
    ])
    imports, classes, functions = smts._walk_tree(root, bytes(t.buf))
    # The namespace itself is captured (as a compact label), NOT dropped.
    assert any("MyNs" in imp for imp in imports), imports
    # The class inside it survives, with its method.
    assert len(classes) == 1, classes
    assert classes[0]["name"] == "Widget"
    assert {m["name"] for m in classes[0]["methods"]} == {"Run"}


def test_namespace_declaration_not_in_import_node_types():
    # Regression guard for the root cause: namespace must not be treated as an
    # import (which would `return` before descending).
    assert "namespace_declaration" not in smts._IMPORT_NODE_TYPES
    assert "namespace_declaration" in smts._NAMESPACE_CONTAINER_NODE_TYPES
    # C#'s real import node is using_directive.
    assert "using_directive" in smts._IMPORT_NODE_TYPES


# ---------------------------------------------------------------------------
# fix-6: on-read wiring of the tree-sitter suffixes
# ---------------------------------------------------------------------------

def test_go_not_supported_when_flag_off(monkeypatch):
    live_sm, live_smts = _live()
    monkeypatch.delenv(live_smts.FLAG_ENV, raising=False)
    # Flag off -> hot path unchanged: .go is not a structure-supported file.
    assert live_sm.is_structure_supported_file("main.go") is False


def test_go_supported_when_flag_on_and_backend_available(monkeypatch):
    live_sm, live_smts = _live()
    monkeypatch.setenv(live_smts.FLAG_ENV, "1")
    # Simulate an installed+usable backend without needing tree_sitter.
    monkeypatch.setattr(live_smts, "is_available", lambda: True)
    assert live_sm.is_structure_supported_file("main.go") is True
    # An unsupported suffix stays False even with the backend on.
    assert live_sm.is_structure_supported_file("notes.xyz") is False


def test_go_supported_false_when_flag_on_but_backend_unavailable(monkeypatch):
    live_sm, live_smts = _live()
    monkeypatch.setenv(live_smts.FLAG_ENV, "1")
    monkeypatch.setattr(live_smts, "is_available", lambda: False)
    assert live_sm.is_structure_supported_file("main.go") is False


def test_summarize_code_source_routes_go_to_tree_sitter(monkeypatch):
    """With the flag on and the backend reporting available, a .go file is
    dispatched to summarize_with_tree_sitter (proving the wiring delivers)."""
    live_sm, live_smts = _live()
    monkeypatch.setenv(live_smts.FLAG_ENV, "1")
    monkeypatch.setattr(live_smts, "is_available", lambda: True)
    sentinel = object()
    calls = {}

    def fake_summ(source, path, **kw):
        calls["hit"] = path
        return sentinel

    monkeypatch.setattr(live_smts, "summarize_with_tree_sitter", fake_summ)
    result = live_sm.summarize_code_source("package main\nfunc main(){}\n", file_path="main.go")
    assert result is sentinel
    assert calls.get("hit") == "main.go"


def test_go_handle_read_no_crash_flag_off(tmp_path):
    """A .go file through the real read_cache hook with the flag off serves
    normally (exit 0, no crash)."""
    import json
    import os
    import subprocess

    target = tmp_path / "main.go"
    target.write_text(
        "package main\n\nimport \"fmt\"\n\nfunc main() {\n    fmt.Println(\"hi\")\n}\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(tmp_path)
    env["TOKEN_OPTIMIZER_READ_CACHE"] = "1"
    env.pop("TOKEN_OPTIMIZER_STRUCTURE_MAP_TREESITTER", None)  # flag OFF
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(target), "offset": 0, "limit": 0},
        "session_id": "go-session-flagoff",
        "agent_id": "go-session-flagoff",
    }
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "read_cache.py")],
        input=json.dumps(payload), capture_output=True, text=True, env=env, timeout=60,
    )
    assert out.returncode == 0, out.stderr
