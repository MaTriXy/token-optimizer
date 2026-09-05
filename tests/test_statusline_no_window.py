#!/usr/bin/env python3
"""Every child_process call in statusline.js must set ``windowsHide: true``.

``statusline.js`` is the Claude Code ``statusLine`` command. The host re-runs it
on EVERY status-line refresh -- every prompt, every tool result, several times a
minute. On Windows, a console child spawned without ``windowsHide`` pops a real
``conhost`` window for its lifetime, so an unhidden spawn here is not a one-time
flash, it is a strobe. That makes this file the loudest possible place for the no-flash fix
to regress.

Node's defaults are the trap: ``windowsHide`` defaults to ``false`` for
``spawn``/``spawnSync``/``execFile``/``execFileSync``, so the flag must be
written out explicitly at every call site. Two extra hazards this test also
covers:

* ``exec``/``execSync`` ALWAYS route through a shell (``cmd.exe /d /s /c`` on
  Windows), so they flash a console even when the target program is windowless.
  ``windowsHide: true`` still hides that ``cmd.exe``, so such a site is fixable
  in place -- but it must never be left bare.
* A ``.cmd``/``.bat`` target, or ``shell: true``, is a ``cmd.exe`` hop by
  definition (since the CVE-2024-27980 fix, Node refuses ``.cmd`` targets
  without ``shell: true``), and therefore always needs ``windowsHide``.

This is a SOURCE SCANNER, not a snapshot of today's single call site: it parses
whichever ``child_process`` APIs the file actually binds and checks every call
of them, so a future edit that adds an unhidden spawn fails here.

Run: python3 -m pytest tests/test_statusline_no_window.py -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
STATUSLINE = SCRIPTS / "statusline.js"

# Every child_process API that can create a process (and therefore a console).
_SPAWNING_APIS = (
    "spawnSync",
    "spawn",
    "execFileSync",
    "execFile",
    "execSync",
    "exec",
    "fork",
)

# APIs that always interpose a shell (cmd.exe on Windows) regardless of options.
_SHELL_APIS = {"exec", "execSync"}


# ---------------------------------------------------------------------------
# Minimal JS lexing: blank comments, then mask string/template CONTENTS.
# Both passes are length-preserving so offsets stay aligned with the raw source.
# ---------------------------------------------------------------------------
def _blank_comments(src: str) -> str:
    """Replace // and /* */ comment bodies with spaces, preserving length.

    Newlines are kept so line numbers survive. String literals are respected so
    a "//" inside a string is not mistaken for a comment.
    """
    out = list(src)
    i, n = 0, len(src)
    quote = None  # active string delimiter, or None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] != "\n":
                out[i] = " "
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            while i < n and not (src[i] == "*" and i + 1 < n and src[i + 1] == "/"):
                if src[i] != "\n":
                    out[i] = " "
                i += 1
            for j in range(i, min(i + 2, n)):
                out[j] = " "
            i += 2
            continue
        i += 1
    return "".join(out)


def _mask_strings(src: str) -> str:
    """Replace string/template literal CONTENTS with 'x', keeping delimiters.

    Length-preserving, so an index into the masked source addresses the same
    character in the raw source. Braces/parens inside literals are neutralised,
    which is what makes the balanced-paren scan below safe.
    """
    out = list(src)
    i, n = 0, len(src)
    quote = None
    while i < n:
        c = src[i]
        if quote:
            if c == "\\":
                if i + 1 < n and src[i + 1] != "\n":
                    out[i] = "x"
                    out[i + 1] = "x"
                i += 2
                continue
            if c == quote:
                quote = None
                i += 1
                continue
            if c != "\n":
                out[i] = "x"
            i += 1
            continue
        if c in "'\"`":
            quote = c
            i += 1
            continue
        i += 1
    return "".join(out)


def _bound_child_process_names(code: str):
    """Return (bare_names, namespace_names) bound from a child_process require/import.

    Handles ``const { a, b: c } = require('child_process')``,
    ``const cp = require('node:child_process')``, and the ESM
    ``import ... from 'child_process'`` forms. Only names actually bound here
    are treated as spawn calls, so an unrelated ``someRegex.exec(...)`` cannot
    trip the scanner.
    """
    bare, namespaces = set(), set()
    mod = r"['\"](?:node:)?child_process['\"]"

    # const { a, b: c } = require('child_process')
    for m in re.finditer(r"\{([^}]*)\}\s*=\s*require\s*\(\s*%s\s*\)" % mod, code):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            name = part.split(":")[-1].strip()
            if name:
                bare.add(name)

    # const cp = require('child_process')
    for m in re.finditer(
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*require\s*\(\s*%s\s*\)" % mod, code
    ):
        namespaces.add(m.group(1))

    # import { a, b as c } from 'child_process'
    for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*%s" % mod, code):
        for part in m.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            name = re.split(r"\s+as\s+", part)[-1].strip()
            if name:
                bare.add(name)

    # import cp from 'child_process' / import * as cp from 'child_process'
    for m in re.finditer(
        r"import\s+(?:\*\s*as\s+)?([A-Za-z_$][\w$]*)\s*from\s*%s" % mod, code
    ):
        namespaces.add(m.group(1))

    return bare, namespaces


def _call_pattern(bare, namespaces):
    """Regex matching a call of a bound child_process API, longest name first."""
    alts = []
    ordered = sorted(_SPAWNING_APIS, key=len, reverse=True)
    for api in ordered:
        if api in bare:
            # Not preceded by '.', so `foo.exec(` never matches a bare binding.
            alts.append(r"(?<![.\w$])" + re.escape(api) + r"\s*\(")
    for ns in sorted(namespaces):
        for api in ordered:
            alts.append(r"(?<![.\w$])" + re.escape(ns) + r"\s*\.\s*" + re.escape(api) + r"\s*\(")
    if not alts:
        return None
    return re.compile("|".join(alts))


def _args_span(masked: str, open_paren_idx: int):
    """Return (start, end) of the argument text inside a balanced paren pair."""
    depth = 0
    i = open_paren_idx
    n = len(masked)
    while i < n:
        c = masked[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                return open_paren_idx + 1, i
        i += 1
    raise AssertionError(
        "unbalanced parentheses after offset %d in statusline.js" % open_paren_idx
    )


def _collect_sites():
    """Parse statusline.js and return one record per child_process call site."""
    raw = STATUSLINE.read_text(encoding="utf-8")
    nocomment = _blank_comments(raw)
    masked = _mask_strings(nocomment)

    # Bindings are parsed from the comment-stripped source, NOT the masked one:
    # masking blanks the 'child_process' module specifier itself.
    bare, namespaces = _bound_child_process_names(nocomment)
    pattern = _call_pattern(bare, namespaces)

    sites = []
    if pattern is None:
        return raw, nocomment, masked, bare, namespaces, sites

    for m in pattern.finditer(masked):
        open_idx = masked.index("(", m.start(), m.end())
        start, end = _args_span(masked, open_idx)
        api = next(a for a in sorted(_SPAWNING_APIS, key=len, reverse=True)
                   if a in m.group(0))
        sites.append({
            "api": api,
            "line": raw.count("\n", 0, m.start()) + 1,
            "args_raw": raw[start:end],
            "args_masked": masked[start:end],
        })
    return raw, nocomment, masked, bare, namespaces, sites


# ---------------------------------------------------------------------------
# Anti-vacuous guards: the scanner must actually be looking at something.
# ---------------------------------------------------------------------------
def test_statusline_exists_and_binds_child_process():
    assert STATUSLINE.exists(), "statusline.js missing at %s" % STATUSLINE
    _, _, masked, bare, namespaces, _ = _collect_sites()
    assert bare or namespaces, (
        "statusline.js binds no child_process API. If the import was renamed or "
        "moved, update _bound_child_process_names() -- do NOT let this test pass "
        "by finding nothing to check."
    )


def test_scanner_finds_at_least_one_call_site():
    """A refactor that hides call sites from the parser must fail, not pass."""
    *_, sites = _collect_sites()
    assert sites, (
        "no child_process call sites found in statusline.js. If the file genuinely "
        "stopped spawning processes, delete this assertion deliberately; otherwise "
        "the scanner is broken and every windowsHide check below is vacuous."
    )


def test_no_call_site_hidden_inside_a_string_literal():
    """Cross-check the masked scan against a comment-stripped-only scan.

    If a call appears only in the comment-stripped source and not in the masked
    source, it lives inside a string literal (a generated script, an eval'd
    snippet) and the scanner is silently skipping it. Fail loudly.
    """
    raw, nocomment, masked, bare, namespaces, sites = _collect_sites()
    pattern = _call_pattern(bare, namespaces)
    if pattern is None:
        pytest.skip("no child_process bindings")
    nocomment_hits = len(pattern.findall(nocomment))
    assert nocomment_hits == len(sites), (
        "found %d child_process call(s) in the comment-stripped source but only "
        "%d after masking string literals: a spawn is embedded in a string "
        "(generated script?) and is NOT being checked for windowsHide."
        % (nocomment_hits, len(sites))
    )


# ---------------------------------------------------------------------------
# The core contract.
# ---------------------------------------------------------------------------
def test_every_child_process_call_sets_windows_hide():
    """No-flash: every spawning call in statusline.js must pass windowsHide: true."""
    *_, sites = _collect_sites()
    offenders = []
    for s in sites:
        if not re.search(r"windowsHide\s*:\s*true", s["args_raw"]):
            offenders.append("line %d: %s(...)" % (s["line"], s["api"]))
    assert not offenders, (
        "child_process call(s) in statusline.js without `windowsHide: true`:\n  "
        + "\n  ".join(offenders)
        + "\n\nstatusline.js runs on EVERY status-line refresh, so an unhidden "
        "console child strobes a cmd window at Windows Desktop-app users. "
        "Add `windowsHide: true` to the options object (merge it, do not replace "
        "cwd/env/timeout/encoding/stdio). If the options come from a shared const "
        "or a spread, inline `windowsHide: true` at the call site so this check "
        "can see it."
    )


def test_shell_and_cmd_hops_are_hidden():
    """`shell: true` and .cmd/.bat targets are cmd.exe hops -- always hide them."""
    *_, sites = _collect_sites()
    offenders = []
    for s in sites:
        args = s["args_raw"]
        is_hop = (
            s["api"] in _SHELL_APIS
            or re.search(r"shell\s*:\s*true", args)
            or re.search(r"\.(?:cmd|bat)\b", args)
        )
        if is_hop and not re.search(r"windowsHide\s*:\s*true", args):
            offenders.append("line %d: %s(...)" % (s["line"], s["api"]))
    assert not offenders, (
        "cmd.exe-hop call site(s) without windowsHide: true:\n  "
        + "\n  ".join(offenders)
        + "\n\nexec/execSync always spawn `cmd.exe /d /s /c` on Windows, and a "
        ".cmd/.bat target needs shell:true (CVE-2024-27980), so these ALWAYS "
        "flash a console unless windowsHide is set."
    )


def test_git_branch_call_still_hidden():
    """Pin the one known site so a plain revert gives a targeted failure.

    statusline.js reads the current git branch on every refresh via
    execFileSync('git', ['branch', '--show-current'], {...}). That is the
    highest-frequency spawn in the whole plugin.
    """
    *_, sites = _collect_sites()
    git_sites = [s for s in sites if re.search(r"['\"]git['\"]", s["args_raw"])]
    assert git_sites, (
        "no git spawn found in statusline.js -- if the branch lookup was removed, "
        "drop this test deliberately."
    )
    for s in git_sites:
        assert re.search(r"windowsHide\s*:\s*true", s["args_raw"]), (
            "the git branch lookup at line %d lost `windowsHide: true` (no-flash revert)"
            % s["line"]
        )
        assert s["api"].startswith("execFile") or s["api"].startswith("spawn"), (
            "the git lookup at line %d uses %s, which routes through cmd.exe on "
            "Windows. Keep it on execFile*/spawn (direct exe, no shell hop)."
            % (s["line"], s["api"])
        )


def test_no_shell_true_in_statusline():
    """Belt-and-braces: this hot-path file should never need a shell at all."""
    *_, sites = _collect_sites()
    for s in sites:
        assert not re.search(r"shell\s*:\s*true", s["args_raw"]), (
            "shell: true at line %d. statusline.js runs on every refresh; a "
            "cmd.exe hop per refresh is pure overhead even when hidden. Use "
            "execFileSync with an argv array instead." % s["line"]
        )
        assert s["api"] not in _SHELL_APIS, (
            "%s at line %d always spawns a shell. Use execFileSync/spawn with an "
            "argv array in this hot path." % (s["api"], s["line"])
        )


# ---------------------------------------------------------------------------
# Runtime leg: real Windows only. Proves Node actually ACCEPTS the options
# object (source presence is not the same as runtime validity) and that the
# script still produces a status line with the git spawn hidden.
# ---------------------------------------------------------------------------
@pytest.mark.skipif(os.name != "nt", reason="runtime console check is Windows-only")
@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_statusline_runs_under_node_on_windows(tmp_path):
    payload = json.dumps({
        "model": {"display_name": "Claude"},
        "workspace": {"current_dir": str(REPO)},
        "context_window": {"used_percentage": 42},
        "session_id": "test-107-session",
    })
    env = dict(os.environ)
    # Redirect the home dir so the script's ~/.claude/token-optimizer sidecar
    # writes land in tmp, never the real profile.
    env["USERPROFILE"] = str(tmp_path)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        ["node", str(STATUSLINE)],
        input=payload.encode("utf-8"),
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    assert proc.returncode == 0, (
        "statusline.js exited %d: %s"
        % (proc.returncode, proc.stderr.decode("utf-8", "replace"))
    )
    assert proc.stdout.strip(), "statusline.js produced no output"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
