#!/usr/bin/env python3
"""first-read retarget contract tests The Read TARGET is never replaced by a skeleton on the active first-read path.
The active cohort now serves the target in full (no ``deny``), emits no
``first_read_skeleton`` measured event for the target, and arms no
target-keyed ``active_fr:`` marker. The periphery-injection hook point
(``_inject_periphery_skeletons``) is a documented no-op until U2 lands, so an
active-cohort first read injects no ``additionalContext`` yet.

Shadow (measure-only) behavior is unchanged: full content + an opportunity
event, no deny. Partial reads and the fail-open path are unchanged.

Run: python3 -m pytest tests/test_read_cache_first_read_retarget.py -v
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
READ_CACHE = SCRIPTS / "read_cache.py"

SESSION = "11111111-1111-1111-1111-111111111111"


def _make_python_file(path: Path, min_bytes: int = 18 * 1024) -> Path:
    """A python file >=16KB whose skeleton is far smaller than its body
    (ratio well over the 0.40 shadow floor), so it clears the active gate."""
    lines = [
        '"""Generated fixture module for first-read retarget tests."""',
        "import os",
        "import sys",
        "from typing import Any, Optional",
        "",
    ]
    # Many small functions with substantial bodies -> signatures-only skeleton
    # is a small fraction of the full file.
    for i in range(120):
        lines.extend(
            [
                f"def func_{i}(self, value: Optional[Any] = None) -> Any:",
                f"    '''Worker function {i} with a multi-line docstring that adds",
                f"    meaningful body content so the full file is well above the",
                f"    16KB shadow floor while the signature-only skeleton stays small.'''",
                f"    accumulator = []",
                f"    for j in range(20):",
                f"        accumulator.append(value if value is not None else j)",
                f"    return sum(accumulator) if accumulator else 0",
                "",
            ]
        )
    text = "\n".join(lines)
    # Pad to the target size if the generated body is short.
    if len(text.encode("utf-8")) < min_bytes:
        text += "\n# " + ("x" * (min_bytes - len(text.encode("utf-8")) - 2)) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def _run_read_cache(snapshot_dir: Path, stdin_payload: dict, extra_env: dict | None = None):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_SNAPSHOT_DIR"] = str(snapshot_dir)
    env.setdefault("TOKEN_OPTIMIZER_READ_CACHE", "1")
    # Active + shadow both default ON; make the test explicit/deterministic.
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_ACTIVE", "1")
    env.setdefault("TOKEN_OPTIMIZER_FIRST_READ_SHADOW", "1")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(READ_CACHE)],
        input=json.dumps(stdin_payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _compression_events(snapshot_dir: Path, session_id: str) -> list[dict]:
    db = snapshot_dir / "trends.db"
    if not db.exists():
        return []
    rows: list[dict] = []
    conn = sqlite3.connect(str(db))
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT feature, command_pattern, tier, detail FROM compression_events "
                "WHERE session_id = ? ORDER BY rowid",
                (session_id,),
            )
        ]
    finally:
        conn.close()
    return rows


def _parse_stdout(out: str) -> dict | None:
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Active cohort: target served full, no deny, no measured skeleton event
# ---------------------------------------------------------------------------

def test_active_cohort_serves_target_full_no_deny(tmp_path):
    f = _make_python_file(tmp_path / "target.py")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(f), "offset": 0, "limit": 0},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    out = _run_read_cache(tmp_path, payload)
    assert out.returncode == 0, out.stderr

    parsed = _parse_stdout(out.stdout)
    # No deny: either no output at all (plain allow) or a response without a
    # "deny" permissionDecision. additionalContext must be absent in U1 (no-op
    # periphery).
    if parsed is not None:
        hso = parsed.get("hookSpecificOutput", {})
        assert hso.get("permissionDecision") != "deny", (
            "active cohort must NOT deny the Read target (retarget)"
        )
        assert "additionalContext" not in hso, (
            "U1 periphery injection is a no-op; no additionalContext expected yet"
        )
    # No measured first_read_skeleton event for the target.
    events = _compression_events(tmp_path, SESSION)
    measured = [
        e for e in events
        if e.get("feature") == "first_read_skeleton" and e.get("tier") == "measured"
    ]
    assert measured == [], (
        f"no first_read_skeleton measured event for the target; got {measured}"
    )


# ---------------------------------------------------------------------------
# Relationships unavailable (U1 no-op) -> no exception, no additionalContext
# ---------------------------------------------------------------------------

def test_periphery_noop_when_relationships_absent(tmp_path):
    f = _make_python_file(tmp_path / "target.py")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(f), "offset": 0, "limit": 0},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    out = _run_read_cache(tmp_path, payload)
    assert out.returncode == 0, out.stderr
    # No exception surfaced to stderr, and no additionalContext injected.
    assert "_inject_periphery_skeletons" not in out.stderr
    parsed = _parse_stdout(out.stdout)
    if parsed is not None:
        assert "additionalContext" not in parsed.get("hookSpecificOutput", {})


def test_inject_periphery_skeletons_is_safe_noop(tmp_path):
    """Direct call: the U1 stub returns silently with no capability present."""
    sys.path.insert(0, str(SCRIPTS))
    try:
        import read_cache  # noqa: WPS433
    finally:
        if str(SCRIPTS) in sys.path:
            sys.path.remove(str(SCRIPTS))
    # Must not raise regardless of arguments; returns None.
    result = read_cache._inject_periphery_skeletons(
        str(tmp_path / "absent.py"), "x = 1\n", "python", SESSION, None, True,
    )
    assert result is None


# ---------------------------------------------------------------------------
# Shadow cohort (non-active language) -> unchanged: full + opportunity, no deny
# ---------------------------------------------------------------------------

def test_shadow_cohort_unchanged_full_and_opportunity(tmp_path):
    # json is structure-supported but NOT in FIRST_READ_ACTIVE_COHORTS, so it
    # takes the shadow (measure-only) path.
    big = tmp_path / "data.json"
    # A large json with repeated structure compresses in the digest path.
    obj = {"items": [{"id": i, "name": f"item_{i}", "payload": "x" * 200} for i in range(200)]}
    big.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(big), "offset": 0, "limit": 0},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    out = _run_read_cache(tmp_path, payload)
    assert out.returncode == 0, out.stderr
    parsed = _parse_stdout(out.stdout)
    if parsed is not None:
        assert parsed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
    # Shadow path emits an opportunity-tier first_read_skeleton event.
    events = _compression_events(tmp_path, SESSION)
    opp = [
        e for e in events
        if e.get("feature") == "first_read_skeleton" and e.get("tier") == "opportunity"
    ]
    assert opp, f"shadow cohort should log an opportunity event; got {events}"


# ---------------------------------------------------------------------------
# Partial read (offset/limit nonzero) -> unchanged full serve, no skeleton path
# ---------------------------------------------------------------------------

def test_partial_read_unchanged(tmp_path):
    f = _make_python_file(tmp_path / "target.py")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(f), "offset": 10, "limit": 50},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    out = _run_read_cache(tmp_path, payload)
    assert out.returncode == 0, out.stderr
    parsed = _parse_stdout(out.stdout)
    if parsed is not None:
        assert parsed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
    # No first_read_skeleton event of any tier for a partial read.
    events = _compression_events(tmp_path, SESSION)
    skel = [e for e in events if e.get("feature") == "first_read_skeleton"]
    assert skel == [], f"partial read must not enter the skeleton path; got {skel}"


# ---------------------------------------------------------------------------
# Fail-open: an exception inside the periphery path still allows the read full
# ---------------------------------------------------------------------------

def test_fail_open_when_periphery_raises(tmp_path):
    f = _make_python_file(tmp_path / "target.py")
    payload = {
        "tool_name": "Read",
        "tool_input": {"file_path": str(f), "offset": 0, "limit": 0},
        "session_id": SESSION,
        "agent_id": SESSION,
    }
    # Force the (no-op) periphery path to raise by monkeypatching via a tiny
    # shim imported before read_cache. We instead simulate a broken store: pass
    # a snapshot dir that becomes read-only so any meta write raises. The
    # retarget path must still return False (serve full) and never deny.
    os.chmod(tmp_path, 0o500)
    try:
        out = _run_read_cache(tmp_path, payload)
    finally:
        os.chmod(tmp_path, 0o755)
    assert out.returncode == 0, out.stderr
    parsed = _parse_stdout(out.stdout)
    if parsed is not None:
        assert parsed.get("hookSpecificOutput", {}).get("permissionDecision") != "deny"
