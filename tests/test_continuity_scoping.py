#!/usr/bin/env python3
"""Per-project scoping for prompt-continuity injection.

A two-project session leaks project A's Key Decisions / Modified Files into a
project B hint because the checkpoint's session-wide fields are dumped verbatim
once the checkpoint clears the same-project gate. The fix is a set-overlap
keep/drop rule (``_keep_recovered_item``) applied to each recovered item BEFORE
the ``[:3]``/``[:5]`` slices, with one disclosure line emitted only when
something is dropped.

These tests cover the three Python surfaces that filter:
  * ``_keep_recovered_item`` / ``_continuity_keep_tokens`` (the decision function)
  * ``build_lean_resume_context`` (the cold-resume-lean block)
  * ``_continuity_prompt_hint`` (the lightweight prompt-continuity hint)

The parity fixture (``PARITY_FIXTURE``) is loaded from a single shared JSON
file (``tests/fixtures/keep_recovered_parity.json``) that is also consumed by
the OpenClaw and OpenCode TS scoping tests, so all three runtimes' keep/drop
decisions are asserted against the SAME token inputs with no copy drift.
"""

from __future__ import annotations

import importlib
import json
import platform
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"


# ---------------------------------------------------------------------------
# Shared parity fixture — single source of truth.
# Loaded from tests/fixtures/keep_recovered_parity.json, consumed by all 3
# suites (Python, OpenClaw TS, OpenCode TS). (item_text, keep_tokens, expected_keep)
# ---------------------------------------------------------------------------

with open(REPO / "tests" / "fixtures" / "keep_recovered_parity.json", encoding="utf-8") as _f:
    PARITY_FIXTURE = [
        (row["item_text"], set(row["keep_tokens"]), row["expected_keep"])
        for row in json.load(_f)
    ]


# Shared path-normalization fixture for _cross_project_file_drop.
# Single source of truth consumed by all 3 suites (Python, OpenClaw TS, OpenCode
# TS). Each row: (path, cwd, expected_drop_case_sensitive, expected_drop_casefold).
# expected_drop_casefold is None for rows whose result is platform-independent.
with open(
    REPO / "tests" / "fixtures" / "cross_project_file_drop_parity.json",
    encoding="utf-8",
) as _f:
    _DROP_ROWS_RAW = json.load(_f)
DROP_FIXTURE = [
    (
        row["path"],
        row["cwd"],
        row["expected_drop"],
        row.get("expected_drop_casefold"),
    )
    for row in _DROP_ROWS_RAW
]

_CASEFOLD_FS = platform.system() in ("Windows", "Darwin")


@pytest.fixture
def measure(monkeypatch):
    """Import measure.py fresh under an isolated snapshot/runtime dir."""
    tmp = tempfile.mkdtemp(prefix="to-103-test-")
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", tmp)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", tmp)
    sys.path.insert(0, str(SCRIPTS))
    for _m in ("measure",):
        if _m in sys.modules:
            del sys.modules[_m]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    cp_dir = Path(tmp) / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CHECKPOINT_DIR", cp_dir, raising=True)
    monkeypatch.setattr(mod, "TRENDS_DB", Path(tmp) / "trends.db", raising=True)
    yield mod, cp_dir
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _write_checkpoint(cp_dir, sid, sidecar):
    """Write a checkpoint .md + .json sidecar with the session id in the filename."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"{sid}-{ts}-auto"
    (cp_dir / f"{base}.md").write_text("# checkpoint\n", encoding="utf-8")
    (cp_dir / f"{base}.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return cp_dir / f"{base}.md"


# ---------------------------------------------------------------------------
# Parity: the decision function on a shared token fixture
# ---------------------------------------------------------------------------

def test_keep_recovered_item_parity_fixture(measure):
    """_keep_recovered_item must match the shared parity fixture exactly."""
    mod, _ = measure
    for item_text, keep_tokens, expected in PARITY_FIXTURE:
        got = mod._keep_recovered_item(item_text, set(keep_tokens))
        assert got is expected, (
            f"item={item_text!r} keep={keep_tokens} expected={expected} got={got}")


def test_keep_recovered_item_no_float_threshold():
    """Sanity: the rule is purely set-overlap, no float math involved."""
    mod = importlib.import_module("measure")
    # A huge item with zero overlap still drops; a tiny item with zero overlap
    # still keeps. No score is computed.
    assert mod._keep_recovered_item("alpha beta gamma delta epsilon zeta", set()) is False
    assert mod._keep_recovered_item("alpha beta", set()) is True


# ---------------------------------------------------------------------------
# Parity: _cross_project_file_drop on the shared path-normalization fixture.
# Covers backslash, UNC, trailing separator, mixed separators,
# case mismatch (casefold on Darwin/Windows), relative, and cwd-absent.
# ---------------------------------------------------------------------------

def test_cross_project_file_drop_parity_fixture():
    """_cross_project_file_drop must match the shared fixture exactly.

    The case-mismatch row has two expectations: expected_drop (case-sensitive,
    Linux) and expected_drop_casefold (case-insensitive, Darwin/Windows).
    """
    mod = importlib.import_module("measure")
    for path, cwd, expected_cs, expected_cf in DROP_FIXTURE:
        expected = expected_cf if (_CASEFOLD_FS and expected_cf is not None) else expected_cs
        got = mod._cross_project_file_drop(path, cwd)
        assert got is expected, (
            f"path={path!r} cwd={cwd!r} casefold={_CASEFOLD_FS} "
            f"expected={expected} got={got}"
        )


# ---------------------------------------------------------------------------
# _neutralize_recovered_body must strip CR (\x0d). The old regex
# [\x00-\x08\x0b\x0c\x0e-\x1f\x7f] kept CR, so Windows \r\n line endings
# survived in the recovered body and CR could be used for terminal injection
# (CR moves the cursor to column 0, letting later text overwrite the start
# of a line). The fix adds \x0d to the strip class.
# ---------------------------------------------------------------------------

def test_neutralize_recovered_body_strips_cr(measure):
    """CR (\x0d) must be stripped from the recovered body."""
    mod, _ = measure
    text = "line1\r\nline2\rXoverwritten"
    result = mod._neutralize_recovered_body(text)
    assert "\r" not in result, (
        f"CR must be stripped from the recovered body; got {result!r}"
    )
    # LF is preserved (body structure):
    assert "\n" in result


def test_neutralize_recovered_body_strips_all_c0_except_tab_and_lf(measure):
    """The strip class covers all C0 controls except tab (\x09) and
    LF (\x0a), now including CR (\x0d)."""
    mod, _ = measure
    # Every C0 control char except \x09 (tab) and \x0a (LF):
    for code in range(0x00, 0x20):
        if code in (0x09, 0x0a):
            continue
        ch = chr(code)
        text = f"before{ch}after"
        result = mod._neutralize_recovered_body(text)
        assert ch not in result, (
            f"C0 control \\x{code:02x} must be stripped; got {result!r}"
        )
    # Tab and LF are preserved:
    assert "\t" in mod._neutralize_recovered_body("a\tb")
    assert "\n" in mod._neutralize_recovered_body("a\nb")


# ---------------------------------------------------------------------------
# build_lean_resume_context — mixed A/B checkpoint queried from B
# ---------------------------------------------------------------------------

def _ab_sidecar(proj_a, proj_b):
    """A checkpoint that touched BOTH project A and project B in one session."""
    return {
        "session_id": "abcdefgh1234",
        "active_task": "work on the beta feature",
        "decisions": [
            # B-overlapping: names beta (cwd basename) -> keep
            "Ship the beta feature behind a feature flag",
            # A-only: names only gamma (project A) -> drop
            "Refactor the gamma delta epsilon module for project alpha",
            # B-overlapping: names a B file stem -> keep
            "Wire beta_router into the request pipeline",
        ],
        "modified_files": [
            {"path": f"{proj_b}/src/beta_router.py"},
            {"path": f"{proj_a}/src/gamma_engine.py"},
        ],
        "recent_reads": [
            f"{proj_a}/docs/gamma_design.md",
            f"{proj_b}/README.md",
        ],
        "git": {"branch": "main", "sha": "abc123"},
    }


def test_lean_resume_keeps_only_b_overlap_and_emits_disclosure(measure):
    """Mixed A/B checkpoint queried from project B drops A-only DECISIONS
    (multi-word, zero overlap with keep_tokens) AND A-only FILE paths
    (cross-project absolute paths not under cwd) and emits exactly ONE
    disclosure line reporting both categories."""
    mod, cp_dir = measure
    proj_b = "/home/u/beta"
    proj_a = "/home/u/gamma"
    _write_checkpoint(cp_dir, "abcdefgh1234", _ab_sidecar(proj_a, proj_b))

    block = mod.build_lean_resume_context(
        "abcdefgh1234", prompt_text="continue the beta work", cwd=proj_b)

    # B-overlapping decisions kept:
    assert "beta feature behind a feature flag" in block
    assert "beta_router" in block
    # A-only DECISION dropped (names only gamma/alpha, zero overlap with
    # keep_tokens={beta, beta_router, ...}):
    assert "gamma delta epsilon" not in block
    # A-only FILE paths dropped (cross-project absolute paths not under cwd):
    assert "gamma_engine" not in block
    assert "gamma_design" not in block
    # B files/reads kept:
    assert "README.md" in block
    # Exactly one disclosure line. Both the A-only decision AND the A-only
    # file paths (modified_files + recent_reads) are dropped, so the
    # disclosure reports both categories:
    assert block.count("- Omitted (scoped to current project):") == 1
    assert "- Omitted (scoped to current project): 1 decision(s), 2 file(s)" in block


def test_lean_resume_single_project_emits_no_disclosure(measure):
    """A single-project checkpoint (everything under cwd) drops nothing and
    emits NO disclosure line. Includes a decision that names NO project token
    so the test genuinely exercises the mixture gate (without the gate the
    token-overlap rule would drop it and mislabel it "different project")."""
    mod, cp_dir = measure
    proj = "/home/u/beta"
    sidecar = {
        "session_id": "abcdefgh1234",
        "active_task": "work on the beta feature",
        "decisions": [
            "Ship the beta feature behind a feature flag",
            "Wire beta_router into the request pipeline",
            # Names NO project token (no beta/beta_router/...): would be
            # dropped by the token-overlap rule alone, but the mixture gate
            # keeps it because the checkpoint is single-project.
            "Switched from REST polling to websocket push",
        ],
        "modified_files": [
            {"path": f"{proj}/src/beta_router.py"},
            {"path": f"{proj}/src/beta_core.py"},
        ],
        "recent_reads": [f"{proj}/README.md"],
        "git": {"branch": "main", "sha": "abc123"},
    }
    _write_checkpoint(cp_dir, "abcdefgh1234", sidecar)

    block = mod.build_lean_resume_context(
        "abcdefgh1234", prompt_text="continue the beta work", cwd=proj)

    assert "beta feature" in block
    assert "beta_router" in block
    assert "beta_core" in block
    # The non-basename decision is kept (single-project -> no filtering):
    assert "Switched from REST polling to websocket push" in block
    assert "- Omitted" not in block


def test_lean_resume_no_filter_when_cwd_absent(measure):
    """Legacy callers (no prompt_text/cwd) get the unfiltered block: A-only
    items survive and NO disclosure line is emitted. Backward compat."""
    mod, cp_dir = measure
    proj_b = "/home/u/beta"
    proj_a = "/home/u/gamma"
    _write_checkpoint(cp_dir, "abcdefgh1234", _ab_sidecar(proj_a, proj_b))

    block = mod.build_lean_resume_context("abcdefgh1234")

    # Everything survives, no disclosure.
    assert "gamma delta epsilon" in block
    assert "gamma_engine" in block
    assert "- Omitted" not in block


# ---------------------------------------------------------------------------
# _continuity_prompt_hint — lightweight hint filters + disclosure
# ---------------------------------------------------------------------------

def test_continuity_prompt_hint_filters_and_discloses(measure, monkeypatch):
    """The lightweight hint applies the same keep/drop filter and emits the
    disclosure line. We force a candidate through the scoring gate by
    stubbing the topic score and same-project check so the rendering path is
    reached deterministically."""
    mod, cp_dir = measure
    proj_b = "/home/u/beta"
    proj_a = "/home/u/gamma"
    _write_checkpoint(cp_dir, "abcdefgh1234", _ab_sidecar(proj_a, proj_b))

    # Force the single checkpoint through: high score, in-project, no external
    # memory (so the full-block threshold stays at 0.5 and 0.9 clears it).
    checkpoints = mod.list_checkpoints()
    assert len(checkpoints) == 1
    cp = checkpoints[0]
    sidecar = mod._read_checkpoint_sidecar(cp["path"])

    monkeypatch.setattr(mod, "_checkpoint_topic_score", lambda text, c, cwd=None: (0.9, sidecar))
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda sc, c: True)
    monkeypatch.setattr(mod, "_external_memory_present", lambda: False)

    # Non-resume-intent prompt so we exercise the LIGHTWEIGHT hint rendering
    # path (the hint file-filter site), not the resume-intent lean-block path
    # that "continue the beta work" would short-circuit into.
    hint = mod._continuity_prompt_hint(
        prompt_text="beta feature",
        session_id="zzzzzzzzzzzz",  # a different session so the checkpoint isn't skipped
        cwd=proj_b)

    assert "beta feature behind a feature flag" in hint
    assert "beta_router" in hint
    # A-only DECISION dropped from the hint:
    assert "gamma delta epsilon" not in hint
    # A-only FILE path dropped (cross-project absolute path not under cwd):
    assert "gamma_engine" not in hint
    # Exactly one disclosure line. The hint surface filters modified_files
    # only (no recent_reads), so both the A-only decision AND the A-only
    # file path are dropped -> disclosure reports both categories:
    assert hint.count("- Omitted (scoped to current project):") == 1
    assert "- Omitted (scoped to current project): 1 decision(s), 1 file(s)" in hint


def test_continuity_prompt_hint_single_project_no_disclosure(measure, monkeypatch):
    """Lightweight hint on a single-project checkpoint emits NO disclosure.
    Uses a non-resume prompt so the lightweight hint rendering path runs
    (not the resume-intent lean block), and includes a decision that names no
    project token so the mixture gate is genuinely exercised."""
    mod, cp_dir = measure
    proj = "/home/u/beta"
    sidecar = {
        "session_id": "abcdefgh1234",
        "active_task": "work on the beta feature",
        "decisions": [
            "Ship the beta feature behind a feature flag",
            # Names NO project token: would be dropped by the token-overlap
            # rule alone, but the mixture gate keeps it (single-project).
            "Switched from REST polling to websocket push",
        ],
        "modified_files": [{"path": f"{proj}/src/beta_router.py"}],
        "recent_reads": [f"{proj}/README.md"],
        "git": {"branch": "main", "sha": "abc123"},
    }
    _write_checkpoint(cp_dir, "abcdefgh1234", sidecar)
    checkpoints = mod.list_checkpoints()
    cp = checkpoints[0]
    sc = mod._read_checkpoint_sidecar(cp["path"])
    monkeypatch.setattr(mod, "_checkpoint_topic_score", lambda text, c, cwd=None: (0.9, sc))
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda s, c: True)
    monkeypatch.setattr(mod, "_external_memory_present", lambda: False)

    hint = mod._continuity_prompt_hint(
        prompt_text="beta feature",
        session_id="zzzzzzzzzzzz",
        cwd=proj)

    assert "beta feature" in hint
    assert "beta_router" in hint
    # The non-basename decision is kept (single-project -> no filtering):
    assert "Switched from REST polling to websocket push" in hint
    assert "- Omitted" not in hint


def test_continuity_prompt_hint_no_filter_when_cwd_absent(measure, monkeypatch):
    """Legacy hint caller with cwd=None gets the UNFILTERED hint: A-only
    decisions and file paths survive and NO disclosure line is emitted
    (legacy behavior). The hint surface previously computed keep_tokens
    unconditionally, so cwd=None callers were token-filtered on prompt text
    alone and got a fabricated "scoped to current project" disclosure."""
    mod, cp_dir = measure
    proj_b = "/home/u/beta"
    proj_a = "/home/u/gamma"
    _write_checkpoint(cp_dir, "abcdefgh1234", _ab_sidecar(proj_a, proj_b))
    checkpoints = mod.list_checkpoints()
    cp = checkpoints[0]
    sidecar = mod._read_checkpoint_sidecar(cp["path"])
    monkeypatch.setattr(mod, "_checkpoint_topic_score", lambda text, c, cwd=None: (0.9, sidecar))
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda s, c: True)
    monkeypatch.setattr(mod, "_external_memory_present", lambda: False)

    hint = mod._continuity_prompt_hint(
        prompt_text="beta feature",
        session_id="zzzzzzzzzzzz",
        cwd=None)

    # A-only decision survives (no token filtering without cwd):
    assert "gamma delta epsilon" in hint
    # A-only file path survives (no path drop without cwd):
    assert "gamma_engine" in hint
    # No fabricated disclosure:
    assert "- Omitted" not in hint


def test_continuity_prompt_hint_no_crash_when_keep_tokens_none(measure, monkeypatch):
    """Site 1 of the hint decision filter must guard
    ``keep_tokens is not None``.

    ``keep_tokens`` is ``... if (text and cwd) else None``. When it is None
    but the checkpoint is multi-project, site 1 ran
    ``_keep_recovered_item(d, None)`` -> ``TypeError: unsupported operand
    type(s) for &: 'set' and 'NoneType'``. The prompt-continuity hook wraps
    the call in ``except Exception: hint=""``, so the symptom was the ENTIRE
    continuity hint vanishing for that turn, silently. Site 2 already guarded;
    site 1 was the lone outlier.

    Through the public entry an empty prompt early-returns before site 1, so
    the None sentinel is simulated here by stubbing ``_continuity_keep_tokens``
    to return None while keeping a non-empty prompt + cwd on a multi-project
    checkpoint. Before the guard this raised TypeError; after, decisions are
    kept verbatim (no filtering on None) with NO disclosure.
    """
    mod, cp_dir = measure
    proj_b = "/home/u/beta"
    proj_a = "/home/u/gamma"
    _write_checkpoint(cp_dir, "abcdefgh1234", _ab_sidecar(proj_a, proj_b))
    checkpoints = mod.list_checkpoints()
    cp = checkpoints[0]
    sidecar = mod._read_checkpoint_sidecar(cp["path"])
    monkeypatch.setattr(mod, "_checkpoint_topic_score", lambda text, c, cwd=None: (0.9, sidecar))
    monkeypatch.setattr(mod, "_checkpoint_in_project", lambda s, c: True)
    monkeypatch.setattr(mod, "_external_memory_present", lambda: False)
    # Force the None sentinel at site 1 (simulates the (text and cwd)-falsy
    # branch without tripping the empty-prompt early return).
    monkeypatch.setattr(mod, "_continuity_keep_tokens", lambda *a, **k: None)

    hint = mod._continuity_prompt_hint(
        prompt_text="beta feature",
        session_id="zzzzzzzzzzzz",
        cwd=proj_b)

    # Did not crash, and decisions are kept verbatim (no token filtering on
    # None -> the decision drop count stays 0):
    assert "gamma delta epsilon" in hint
    # The cross-project FILE path is still path-dropped (that rule is
    # path-based, independent of keep_tokens), so the disclosure reports the
    # file drop ONLY -- it must NOT claim a decision was dropped, because no
    # decision was filtered on the None sentinel:
    assert "- Omitted (scoped to current project): 1 file(s)" in hint
    assert "decision(s)" not in hint
