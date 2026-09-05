"""#107 follow-up: the dashboard openers must never hand a path to cmd's parser.

The first #107 fix replaced ``subprocess.run(["start", path], shell=True)`` /
``execFile("start", ...)`` with ``cmd /c start "" <path>``. That removed the
console flash and reintroduced something worse: **command injection**.

Dropping ``shell=True`` removes PYTHON's shell, not cmd.exe's PARSER. Whoever
builds the Windows command line -- CPython's ``subprocess.list2cmdline`` or
Node/libuv's ``quote_cmd_arg`` -- quotes an argument only when it contains a
space, a tab, a double quote, or is empty. Neither quotes on ``&``, ``^``,
``|``, ``(`` or ``)``::

    >>> subprocess.list2cmdline(
    ...     ["cmd", "/c", "start", "", r"C:\\Users\\R&D\\...\\dashboard.html"])
    'cmd /c start "" C:\\Users\\R&D\\...\\dashboard.html'      # UNQUOTED

``&`` is a LEGAL Windows local-account-name character (the illegal set is
``" / \\ [ ] : ; | = , + * ? < >``), so ``C:\\Users\\R&D\\...`` is a path a real
user really has. cmd.exe splits that line at the ``&`` and executes the tail as
a SECOND command, resolved against the current working directory. ``%VAR%``
expands even inside double quotes, and the bare image name ``cmd`` is resolved
CWD-FIRST (libuv ``search_path``; ``CreateProcessW`` with a NULL
``lpApplicationName``), so a planted ``cmd.exe`` in an untrusted working
directory would have been executed -- hidden by our own no-window flags.

The path is not a constant: it is derived from ``CLAUDE_CONFIG_DIR`` (which
``runtime_env.claude_home`` deliberately accepts pointing anywhere) and from
``HOME``/``USERPROFILE``.

The fixes, and what this file pins:

* Python (``fleet.py``): ``os.startfile(path)`` -- ShellExecuteW, the path is a
  real ARGUMENT, no command line is built, no console is allocated. The same
  helper ``measure.py::_open_in_browser`` has always used.
* Node (``openclaw/src/cli.ts`` + ``dist/cli.js``): Node has no ``os.startfile``,
  so ``rundll32 url.dll,FileProtocolHandler <path>`` -- ShellExecute again, one
  real argv entry, a GUI-subsystem exe, addressed absolutely under
  ``%SystemRoot%`` so it cannot be resolved out of the current directory.

Companion pins live in ``tests/test_hooks_root_no_window.py``
(fleet opener shape) and ``tests/test_runtimes_no_window.py`` (src/dist
parity). This file is the one that states the SECURITY property directly, both
by source scan and by behaviour.

KNOWN GAP, deliberately not asserted here: ``opencode/src/tools/dashboard.ts``
still ships ``execFileSync("cmd", ["/c", "start", "", outputPath], hide)`` --
the identical injectable shape in a third opener that this lane does not own.
Add it to ``_JS_OPENERS`` when that file is fixed.

Run: python3 -m pytest tests/test_opener_injection.py -v
"""
from __future__ import annotations

import ast
import os
import platform
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FLEET = REPO / "skills" / "fleet-auditor" / "scripts" / "fleet.py"
CLI_SRC = REPO / "openclaw" / "src" / "cli.ts"
CLI_DIST = REPO / "openclaw" / "dist" / "cli.js"
OPENCODE_DASH = REPO / "opencode" / "src" / "tools" / "dashboard.ts"

# The third opener (opencode) was found during the Cluster C fix and carried the
# identical injectable `cmd /c start "" <path>` shape. opencode ships src/
# directly (no dist), so it is guarded here in its shipped form.
_JS_OPENERS = {
    "openclaw/src/cli.ts": CLI_SRC,
    "openclaw/dist/cli.js": CLI_DIST,
    "opencode/src/tools/dashboard.ts": OPENCODE_DASH,
}

# A path a real Windows user really has: `&` is legal in an account name, and
# there is no space anywhere, so no argv builder will quote it.
HOSTILE_PATH = r"C:\Users\R&D\_backups\token-optimizer\fleet-dashboard.html"


def _fleet_module():
    """Import ``fleet.py`` the way its own scripts dir does."""
    scripts = str(FLEET.parent)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import fleet  # noqa: PLC0415 -- path must be set up first

    return fleet


def _fake_os(**overrides) -> types.ModuleType:
    """A shallow copy of the real ``os`` module with attributes overridden.

    Patching the real ``os`` module in place is avoided on purpose: pathlib
    dispatches its path flavour on ``os.name``, so mutating it breaks unrelated
    machinery on the POSIX legs of CI.
    """
    fake = types.ModuleType("os_fake")
    fake.__dict__.update(os.__dict__)
    fake.__dict__.update(overrides)
    return fake


def _no_spawn_subprocess() -> types.ModuleType:
    """A ``subprocess`` stand-in whose every spawn entry point fails the test."""
    fake = types.ModuleType("subprocess_fake")
    fake.__dict__.update(subprocess.__dict__)

    def _forbidden(*args, **kwargs):
        raise AssertionError(
            "the Windows opener spawned a process instead of calling "
            f"os.startfile: {args!r} {kwargs!r}"
        )

    for name in ("run", "Popen", "call", "check_call", "check_output"):
        setattr(fake, name, _forbidden)
    return fake


# ---------------------------------------------------------------------------
# 1. Source scan -- neither opener may name a command interpreter.
# ---------------------------------------------------------------------------
_INTERPRETERS = {"cmd", "cmd.exe", "start", "powershell", "powershell.exe", "pwsh"}


def test_fleet_opener_argv_never_names_a_command_interpreter():
    """No ``subprocess`` call in fleet.py may pass a path to cmd/start.

    AST-based, whole file: the injectable shape must not come back anywhere,
    including in a new helper someone adds beside ``_open_in_browser``.
    """
    tree = ast.parse(FLEET.read_text(encoding="utf-8"), filename=str(FLEET))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (
            isinstance(fn, ast.Attribute)
            and fn.attr in {"run", "Popen", "call", "check_call", "check_output"}
            and isinstance(fn.value, ast.Name)
            and fn.value.id in {"subprocess", "_subprocess"}
        ):
            continue
        literals = [
            e.value
            for a in node.args
            if isinstance(a, ast.List)
            for e in a.elts
            if isinstance(e, ast.Constant) and isinstance(e.value, str)
        ]
        named = {v.lower() for v in literals} & _INTERPRETERS
        if named:
            offenders.append(f"fleet.py:{node.lineno} -- {sorted(named)}")
    assert not offenders, (
        "a filesystem path routed through cmd.exe's parser is command "
        "injection, not a quoting nit -- see this module's docstring:\n  "
        + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("label", sorted(_JS_OPENERS))
def test_js_opener_never_spawns_a_command_interpreter(label):
    """``execFile``/``execFileSync``/``spawn`` must not name cmd in the JS lane."""
    path = _JS_OPENERS[label]
    if not path.is_file():
        pytest.skip(f"{label} not present in this checkout")
    text = path.read_text(encoding="utf-8")
    # Only executable positions: `execFile("cmd", ...)`, `spawn('cmd.exe', ...)`.
    # The optional `)` before the call parens matches tsc's CommonJS emit,
    # `(0, child_process_1.execFile)("cmd", ...)` -- without it this scan reads
    # src/cli.ts and is blind to the artifact that actually ships.
    pattern = re.compile(
        r"""\b(?:execFile|execFileSync|spawn|spawnSync|exec)\s*\)?\s*\(\s*["'](cmd(?:\.exe)?|powershell(?:\.exe)?|pwsh)["']""",
        re.IGNORECASE,
    )
    hits = pattern.findall(text)
    assert not hits, (
        f"{label} spawns {hits} with the dashboard path as an argument. libuv "
        "quotes only on space/tab/quote, so `&` in a path (legal in a Windows "
        "account name) reaches cmd's parser and the tail runs as a command."
    )


def test_js_opener_addresses_its_launcher_absolutely():
    """A bare image name is resolved CWD-FIRST on Windows (libuv ``search_path``).

    The launcher must therefore be an absolute path under ``%SystemRoot%``, or a
    planted binary in an untrusted working directory wins -- silently, because
    ``windowsHide`` hides exactly that hop.
    """
    for label, path in sorted(_JS_OPENERS.items()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "url.dll,FileProtocolHandler" in text, (
            f"{label} lost the rundll32 shell-open trampoline"
        )
        assert "System32\\\\rundll32.exe" in text, (
            f"{label} must build an absolute rundll32 path from %SystemRoot%"
        )
        assert "SystemRoot" in text, f"{label} must read %SystemRoot%"


# How each opener surfaces a failure. openclaw's execFile is async with an error
# callback, so it needs an explicit message. opencode's opener is execFileSync
# inside the tool handler's try/catch, which returns the thrown message to the
# user as a "Dashboard Error" result -- a different mechanism, equally loud.
# The invariant under test is "a failed open is visible", not one literal string.
_JS_FAILURE_MARKERS = {
    "openclaw/src/cli.ts": "Could not open a browser",
    "openclaw/dist/cli.js": "Could not open a browser",
    "opencode/src/tools/dashboard.ts": "Dashboard Error",
}


def test_js_opener_reports_failures():
    """#107 hid for months behind a noop callback. Every opener must speak now."""
    for label, path in sorted(_JS_OPENERS.items()):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        marker = _JS_FAILURE_MARKERS[label]
        assert marker in text, (
            f"{label} swallows opener errors again -- that silence is what let "
            "the ENOENT ship undetected."
        )


# ---------------------------------------------------------------------------
# 2. Behaviour -- fleet.py's Windows branch, with a hostile path.
# ---------------------------------------------------------------------------
def test_fleet_windows_branch_passes_ampersand_path_intact(monkeypatch):
    """The `&` path arrives at ShellExecuteW as ONE unmodified argument.

    This is the whole security property: one argument, byte-identical to the
    path we meant to open, never concatenated into a command string.
    """
    fleet = _fleet_module()
    seen = []
    monkeypatch.setattr(fleet, "os", _fake_os(startfile=lambda p: seen.append(p)))
    monkeypatch.setattr(fleet, "subprocess", _no_spawn_subprocess())
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    fleet._open_in_browser(Path(HOSTILE_PATH))

    assert len(seen) == 1, f"expected exactly one open call, got {seen!r}"
    (opened,) = seen
    assert isinstance(opened, str)
    assert opened == HOSTILE_PATH, (
        f"the path was rewritten on its way to the opener: {opened!r}"
    )
    # The tell-tales of a command line: interpreter names, separators, quoting.
    assert "&" in opened, "the fixture must keep its metacharacter"
    for token in ("cmd", "/c", "start", '""'):
        assert token not in opened, (
            f"{token!r} appeared in the argument -- a command line is being "
            "built again"
        )


def test_fleet_windows_branch_spawns_no_process_at_all(monkeypatch):
    """``os.startfile`` is not merely preferred, it is the only thing allowed.

    Any ``subprocess`` entry point on the Windows branch raises from the fake
    module above, so a "just one small cmd hop" regression fails loudly rather
    than passing the argv scan on a technicality.
    """
    fleet = _fleet_module()
    calls = []
    monkeypatch.setattr(fleet, "os", _fake_os(startfile=lambda p: calls.append(p)))
    monkeypatch.setattr(fleet, "subprocess", _no_spawn_subprocess())
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    fleet._open_in_browser(Path(HOSTILE_PATH))
    assert calls == [HOSTILE_PATH]


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX open/xdg-open branch; a POSIX-style path becomes a "
    "backslash WindowsPath under str() on Windows, so the argv assertion is "
    "meaningful only on POSIX",
)
@pytest.mark.parametrize(
    "system,expected",
    [("Darwin", "open"), ("Linux", "xdg-open")],
)
def test_fleet_posix_branches_pass_the_path_as_one_argv_entry(
    monkeypatch, system, expected
):
    """POSIX keeps ``open``/``xdg-open`` -- argv lists, no shell, flag intact."""
    fleet = _fleet_module()
    recorded = {}

    fake_sub = types.ModuleType("subprocess_record")
    fake_sub.__dict__.update(subprocess.__dict__)

    def _run(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0)

    fake_sub.run = _run
    monkeypatch.setattr(fleet, "subprocess", fake_sub)
    monkeypatch.setattr(platform, "system", lambda: system)

    hostile_posix = "/tmp/R&D/fleet-dashboard.html"
    fleet._open_in_browser(Path(hostile_posix))

    assert recorded["argv"] == [expected, hostile_posix]
    assert "shell" not in recorded["kwargs"], "no shell on any branch"
    assert recorded["kwargs"].get("creationflags") == 0, (
        "CREATE_NO_WINDOW is 0 off Windows and must still be passed uniformly"
    )


def test_fleet_opener_failure_is_visible(monkeypatch, capsys, tmp_path):
    """T6-L1: the swallowed error is what hid #107. Failures must print.

    ``check=False`` with a discarded returncode meant a broken opener looked
    exactly like a working one. Now any failure prints a manual-open URL.

    The path must be ABSOLUTE: ``Path.as_uri()`` (used to build the hint)
    rejects a driveless path on Windows with ValueError, and a POSIX-style
    ``/tmp/...`` literal is driveless there. ``tmp_path`` is absolute on both
    platforms; the ``R&D`` segment keeps the ``&`` so the %26-encoding of the
    real filename is still exercised.
    """
    fleet = _fleet_module()

    def _boom(_path):
        raise OSError("no association for .html")

    monkeypatch.setattr(fleet, "os", _fake_os(startfile=_boom))
    monkeypatch.setattr(fleet, "subprocess", _no_spawn_subprocess())
    monkeypatch.setattr(platform, "system", lambda: "Windows")

    dashboard = tmp_path / "R&D" / "fleet-dashboard.html"
    expected_uri = dashboard.as_uri()
    assert "R%26D" in expected_uri, "the & must be percent-encoded in the hint"

    fleet._open_in_browser(dashboard)
    out = capsys.readouterr().out
    assert "Could not auto-open browser" in out
    assert expected_uri in out, "the manual-open hint must name the real file"


def test_fleet_opener_rejects_unknown_platforms(monkeypatch, capsys, tmp_path):
    """An unrecognised platform must report, not silently do nothing.

    Uses an absolute ``tmp_path`` so the manual-open hint's ``Path.as_uri()``
    does not raise ValueError on Windows (driveless paths are rejected there).
    """
    fleet = _fleet_module()
    monkeypatch.setattr(fleet, "subprocess", _no_spawn_subprocess())
    monkeypatch.setattr(platform, "system", lambda: "Plan9")

    fleet._open_in_browser(tmp_path / "fleet-dashboard.html")
    assert "Could not auto-open browser" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 3. The dashboard file itself is published atomically (T6-M3).
# ---------------------------------------------------------------------------
def test_fleet_dashboard_write_is_atomic():
    """Truncate-in-place + an async opener = the browser can read a torn page.

    A second invocation reopening the fixed path with ``"w"`` truncates the file
    the first invocation's browser is still resolving, and on Windows a reader
    holding it without FILE_SHARE_WRITE makes the truncate raise. The write must
    land on a temp sibling and be renamed onto the final path.
    """
    src = FLEET.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(FLEET))
    direct = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "open"
        and node.args
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "FLEET_DASHBOARD_PATH"
    ]
    assert not direct, (
        f"fleet.py writes FLEET_DASHBOARD_PATH in place at line(s) {direct}; "
        "write a temp sibling and os.replace it onto the path"
    )
    assert "os.replace(_tmp_dashboard, FLEET_DASHBOARD_PATH)" in src, (
        "the dashboard must be published with an atomic rename"
    )
