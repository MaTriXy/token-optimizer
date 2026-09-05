"""#107 -- Windows console-flash guard for the JS/TS runtimes.

``tests/test_windows_spawn_no_window.py`` covers the Python surface
(``measure.py``, ``hooks/run.py``, the bridges). This file is its counterpart
for the shipped JavaScript/TypeScript runtimes, which have the same failure
mode and none of the same machinery:

  * ``openclaw/`` -- the ``token-optimizer`` bin and the OpenClaw extension
    entry. ``src/*.ts`` is the source of truth; ``dist/*.js`` is the committed
    ``tsc`` artifact that actually ships (see
    ``tests/test_openclaw_dist_freshness.py``). BOTH are scanned: a fix that
    lands only in src ships as a no-op, and a fix that lands only in dist is
    reverted by the next ``bun run build``.
  * ``opencode/src/**`` -- the OpenCode plugin, loaded in-process in the host.
  * ``vscode-extension/src/**``, ``copilot/``, ``hermes/`` -- CLEAN today
    (zero process spawns); pinned so a new spawn cannot be added silently.

On Windows, a child console application spawned without ``windowsHide`` gets a
fresh console allocated for it, which flashes a cmd window on the user's
Desktop. In Node that is one option away: every ``child_process`` API
(``spawn``/``spawnSync``/``exec``/``execSync``/``execFile``/``execFileSync``)
takes ``windowsHide``, and libuv turns it into ``CREATE_NO_WINDOW`` -- the same
flag the Python side sets by hand.

Three regressions this file is built to catch:

  1. A new ``child_process`` call site added without ``windowsHide``.
  2. The site-#1 bug: ``execFile("start", [path])``. ``start`` is a cmd.exe
     BUILTIN -- there is no ``start.exe`` -- so ``execFile`` raised ENOENT into
     a callback that ignored it, and the Windows dashboard silently never
     opened. It must be spawned as ``cmd /c start "" <path>``, hidden.
  3. A ``Bun.spawn`` added to the bun-based ``opencode`` package, which a
     scanner that only knows ``child_process`` would walk straight past. Bun's
     spawn API has no ``windowsHide`` equivalent, so it is rejected outright on
     this surface rather than waved through.

Run: python3 -m pytest tests/test_runtimes_no_window.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Trees that legitimately spawn child processes.
SPAWNING_TREES = (
    REPO / "openclaw" / "src",
    REPO / "openclaw" / "dist",
    REPO / "openclaw" / "hooks",
    REPO / "opencode" / "src",
)

# Trees that must contain NO process-spawn code at all.
CLEAN_TREES = (
    REPO / "vscode-extension" / "src",
    REPO / "copilot",
    REPO / "hermes",
)

SOURCE_SUFFIXES = (".ts", ".js", ".mjs", ".cjs")

# Node child_process spawn APIs. ``fork`` is included: it spawns a real child.
SPAWN_APIS = (
    "spawn",
    "spawnSync",
    "exec",
    "execSync",
    "execFile",
    "execFileSync",
    "fork",
)

_CALL_RE = re.compile(
    r"(?P<prefix>[\w$.]*?)\b(?P<api>" + "|".join(sorted(SPAWN_APIS, key=len, reverse=True)) + r")\s*\("
)

# tsc's CommonJS emit rewrites `execFile(a, b)` into `(0, child_process_1.execFile)(a, b)`.
# Normalize that back to a plain call so one scanner handles src and dist alike.
_CJS_CALL_RE = re.compile(r"\(\s*0\s*,\s*[\w$]+\.([\w$]+)\s*\)\s*\(")

# A shared options const, e.g. `const hide = { windowsHide: true } as const;`.
# Passing that identifier is just as safe as an inline literal, so the scanner
# resolves same-file bindings instead of dictating a call-site style. The
# binding must actually contain `windowsHide` to count.
_HIDE_CONST_RE = re.compile(
    r"\b(?:const|let|var)\s+([\w$]+)\s*(?::[^=;\n]*)?=\s*\{[^{}]*\bwindowsHide\b[^{}]*\}"
)


def _iter_sources(tree: Path):
    """Yield real source files under *tree* (no node_modules, maps, or .d.ts)."""
    if not tree.is_dir():
        return
    for path in sorted(tree.rglob("*")):
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        if "node_modules" in path.parts:
            continue
        if path.name.endswith(".d.ts") or path.name.endswith(".map"):
            continue
        yield path


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO))


def _arg_list(text: str, open_paren: int) -> str:
    """Return the argument text of the call whose '(' is at *open_paren*.

    Tracks nesting and string/template literals so a ')' inside a string or a
    nested call does not end the scan early. Multi-line calls are covered.
    """
    depth = 0
    i = open_paren
    quote = ""
    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "\"'`":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_paren + 1:i]
        i += 1
    return text[open_paren + 1:]


def _bindings(text: str) -> dict[str, str]:
    """Map ``const/let/var NAME = <expr>`` to the expression text.

    The original #107 bug never wrote ``execFile("start", ...)`` literally --
    it wrote ``execFile(opener, ...)`` where ``opener`` was a platform ternary
    that could evaluate to "start". A guard that only inspects the literal at
    the call site passes on that exact revert, so resolve one hop.
    """
    out: dict[str, str] = {}
    for match in re.finditer(
        r"\b(?:const|let|var)\s+([\w$]+)\s*(?::[^=;\n]*)?=\s*([^;\n]+)", text
    ):
        out.setdefault(match.group(1), match.group(2))
    return out


def _spawn_call_sites(path: Path):
    """Yield ``(line_no, api, args_text)`` for each child_process spawn call.

    Only files that reference ``child_process`` are scanned, which keeps bare
    ``db.exec("PRAGMA ...")`` (bun:sqlite) out of the results. Within such a
    file, a dotted call is accepted only when its receiver names child_process
    -- ``foo.exec(...)`` on some unrelated object is not a spawn.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    if "child_process" not in text:
        return
    normalized = _CJS_CALL_RE.sub(lambda m: m.group(1) + "(", text)
    hide_consts = set(_HIDE_CONST_RE.findall(normalized))
    for match in _CALL_RE.finditer(normalized):
        prefix = match.group("prefix")
        if prefix.endswith(".") and "child_process" not in prefix:
            # e.g. ``this.db.exec(`` -- a SQL call, not a spawn.
            continue
        open_paren = normalized.index("(", match.end() - 1)
        args = _arg_list(normalized, open_paren)
        line_no = normalized.count("\n", 0, match.start("api")) + 1
        # Splice in any resolved hide-const so callers can test `args` alone.
        resolved = args
        if "windowsHide" not in resolved:
            arg_tokens = set(re.findall(r"[\w$]+", args))
            if hide_consts & arg_tokens:
                resolved += "  /* windowsHide via " + ",".join(
                    sorted(hide_consts & arg_tokens)
                ) + " */"
        yield line_no, match.group("api"), resolved


# ---------------------------------------------------------------------------
# 1. Every child_process spawn site must pass windowsHide.
# ---------------------------------------------------------------------------
def test_every_child_process_spawn_passes_windows_hide() -> None:
    present = [t for t in SPAWNING_TREES if t.is_dir()]
    if not present:
        pytest.skip("no JS/TS runtime trees in this checkout")

    scanned = 0
    offenders: list[str] = []
    for tree in present:
        for path in _iter_sources(tree):
            for line_no, api, args in _spawn_call_sites(path):
                scanned += 1
                if "windowsHide" not in args:
                    offenders.append(f"{_rel(path)}:{line_no} -- {api}(...)")

    assert scanned > 0, (
        "the scanner found ZERO child_process call sites -- it has stopped "
        "matching real code (a refactor, a rename, or a moved tree). Fix the "
        "scanner; a silently empty guard is worse than no guard."
    )
    assert not offenders, (
        "child_process spawn sites missing `windowsHide: true` (#107: each one "
        "flashes a console window on a Windows Desktop host). Merge the option "
        "into the existing options object, do not clobber it:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 2. `start` is a cmd.exe builtin, never an executable.
# ---------------------------------------------------------------------------
def test_no_bare_start_opener_passed_as_executable() -> None:
    """#107 site-1: ``execFile("start", [path])`` is ENOENT on Windows.

    There is no ``start.exe``. Passing "start" as the command silently fails
    (the openclaw dashboard opener swallowed the error), so the browser never
    opened. The correct spawn is ``cmd /c start "" <path>`` with
    ``windowsHide: true``. This test fails if the bare-"start" opener returns.
    """
    present = [t for t in SPAWNING_TREES if t.is_dir()]
    if not present:
        pytest.skip("no JS/TS runtime trees in this checkout")

    literal_start = re.compile(r"""["']start["']""")
    offenders: list[str] = []
    for tree in present:
        for path in _iter_sources(tree):
            text = path.read_text(encoding="utf-8", errors="replace")
            binds = _bindings(text)
            for line_no, api, args in _spawn_call_sites(path):
                # The command is the FIRST argument. A "start" literal inside
                # the args ARRAY (`cmd /c start ""`) is the correct form and
                # must not be flagged -- only the command slot is inspected.
                command = args.split(",", 1)[0].strip()
                if literal_start.fullmatch(command):
                    offenders.append(f"{_rel(path)}:{line_no} -- {api}('start', ...)")
                elif literal_start.search(command):
                    # Inline ternary/expression yielding "start".
                    offenders.append(
                        f"{_rel(path)}:{line_no} -- {api}(<expr yielding 'start'>, ...)"
                    )
                elif re.fullmatch(r"[\w$]+", command):
                    # Bare identifier: resolve one binding hop. This is the
                    # shape the original bug actually had (`opener`).
                    bound = binds.get(command, "")
                    if literal_start.search(bound):
                        offenders.append(
                            f"{_rel(path)}:{line_no} -- {api}({command}, ...) "
                            f"where {command} can be 'start'"
                        )

    assert not offenders, (
        "'start' is a cmd.exe BUILTIN, not an executable -- spawning it "
        "directly raises ENOENT on Windows and the failure is usually "
        "swallowed. Use execFile('cmd', ['/c', 'start', '', path], "
        "{ windowsHide: true }) instead:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 3. No Bun.spawn on this surface (it has no windowsHide equivalent).
# ---------------------------------------------------------------------------
def test_no_bun_spawn_on_runtime_surface() -> None:
    """``opencode`` is a bun package, so ``Bun.spawn`` is a live temptation.

    Bun's spawn API exposes no ``windowsHide``/``CREATE_NO_WINDOW`` option, so
    a Bun.spawn'd console child would flash with no way to suppress it from
    the call site. Until that changes upstream, this surface routes every spawn
    through ``node:child_process``, which bun implements and which does support
    ``windowsHide``.
    """
    present = [t for t in SPAWNING_TREES if t.is_dir()]
    if not present:
        pytest.skip("no JS/TS runtime trees in this checkout")

    offenders: list[str] = []
    pattern = re.compile(r"\bBun\s*\.\s*(spawn|spawnSync)\s*\(")
    for tree in present:
        for path in _iter_sources(tree):
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in pattern.finditer(text):
                line_no = text.count("\n", 0, match.start()) + 1
                offenders.append(f"{_rel(path)}:{line_no} -- Bun.{match.group(1)}(")

    assert not offenders, (
        "Bun.spawn has no windowsHide equivalent, so a console child spawned "
        "through it flashes a window on Windows with no per-call fix. "
        "Use `await import('node:child_process')` with `windowsHide: true`:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 4. CLEAN components stay clean.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "tree_name",
    ["vscode-extension/src", "copilot", "hermes"],
)
def test_clean_components_have_no_spawn_code(tree_name: str) -> None:
    """These three ship no process-spawn code today (#107 census).

    ``vscode-extension`` is pure filesystem reads + UI; ``copilot/`` is a
    README; ``hermes/`` delegates every subprocess to
    ``skills/token-optimizer/scripts/hermes_hook_bridge.py`` (covered by
    ``test_windows_spawn_no_window.py``). If a spawn is added here it must be
    added to the #107 census and given ``windowsHide`` first -- which means
    moving the tree into SPAWNING_TREES above, deliberately.
    """
    tree = REPO / tree_name
    if not tree.is_dir():
        pytest.skip(f"{tree_name} not present in this checkout")

    offenders: list[str] = []
    for path in _iter_sources(tree):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "child_process" in text:
            offenders.append(f"{_rel(path)} -- imports child_process")
        if re.search(r"\bBun\s*\.\s*spawn", text):
            offenders.append(f"{_rel(path)} -- Bun.spawn")

    assert not offenders, (
        f"{tree_name} was CLEAN in the #107 spawn census and has grown spawn "
        "code. Give every new call site `windowsHide: true` and move the tree "
        "into SPAWNING_TREES in this file so it is scanned:\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 5. src/dist parity for the one openclaw spawn file that ships.
# ---------------------------------------------------------------------------
def test_openclaw_dist_cli_carries_the_windows_opener_fix() -> None:
    """``dist/cli.js`` is what ships; ``src/cli.ts`` is only what builds it.

    ``test_openclaw_dist_freshness.py`` proves a dist artifact EXISTS for each
    source file, not that it carries current logic. This pins the #107 marker
    specifically, the same way that file pins the #103 markers -- a src-only
    fix that was never rebuilt fails here instead of shipping as a no-op.

    The marker used to be ``"/c", "start", ""``. Do NOT re-pin that: the
    ``cmd /c start "" <path>`` shape is command injection. libuv's
    ``quote_cmd_arg`` quotes an argument only when it holds a space, tab or
    quote -- never on ``&``, ``^``, ``|``, ``(``, ``)`` -- and ``&`` is a legal
    Windows account-name character, so a real path like
    ``C:\\Users\\R&D\\.openclaw\\token-optimizer\\dashboard.html`` reached
    cmd.exe UNQUOTED; cmd split it at the ``&`` and executed the tail as a
    second command relative to the CWD. ``%VAR%`` expands even inside quotes,
    and the bare image name ``cmd`` resolves CWD-FIRST via libuv's
    ``search_path``, so a planted ``cmd.exe`` would have run windowless. The
    path comes from HOME/USERPROFILE.

    The shipped shape is ``rundll32 url.dll,FileProtocolHandler <path>``:
    ShellExecute with the path as a real argv entry, no command interpreter, no
    parser, no console. ``rundll32`` is addressed absolutely under
    ``%SystemRoot%`` so it cannot be picked up from the current directory.
    ``windowsHide`` still applies to that trampoline only -- the browser it
    hands off to keeps its own SW_SHOWNORMAL and stays visible.
    """
    dist_cli = REPO / "openclaw" / "dist" / "cli.js"
    src_cli = REPO / "openclaw" / "src" / "cli.ts"
    if not dist_cli.is_file() or not src_cli.is_file():
        pytest.skip("openclaw cli src/dist not present in this checkout")

    dist_text = dist_cli.read_text(encoding="utf-8")
    src_text = src_cli.read_text(encoding="utf-8")

    marker = '"url.dll,FileProtocolHandler", filepath'
    for label, text in (("src/cli.ts", src_text), ("dist/cli.js", dist_text)):
        assert marker in text, (
            f"openclaw {label} is missing the #107 Windows opener "
            "(`rundll32 url.dll,FileProtocolHandler <path>`). If only dist is "
            "missing it, run `npm run build` in openclaw/ and commit the "
            "regenerated dist."
        )
        assert "System32\\\\rundll32.exe" in text, (
            f"openclaw {label} must address rundll32 absolutely under "
            "%SystemRoot%; a bare image name is resolved CWD-first on Windows."
        )
        assert "windowsHide" in text, (
            f"openclaw {label} lost windowsHide -- the launcher hop will flash."
        )
        assert '"/c", "start", ""' not in text, (
            f"openclaw {label} re-pinned the injectable `cmd /c start \"\"` "
            "opener -- see this test's docstring."
        )
