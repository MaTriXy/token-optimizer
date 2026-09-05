"""_probe_windowsapps_candidate() must decide liveness from the
CANDIDATE itself, not from a sibling "twin" (pythonw.exe).

In WindowsApps each App Execution Alias name is claimed independently, so a LIVE
pythonw.exe twin can sit beside a DEAD python3.exe redirector stub from a
different package. The old probe wrote its proof-of-life marker with the twin, so
a live twin made the dead candidate look alive; the dead stub then got cached and
exec'd on every hook, silently failing while a working Python sat later in PATH.

These tests drive the real bash function in isolation with fake candidates:
- a LIVE candidate (writes the marker) must be accepted (rc 0),
- a DEAD stub (ignores -c, no marker, but exits 0 on --version like the real Store
  redirector) must be REJECTED (rc != 0) -- proving the fix does not fall through
  to the `--version` probe the stub false-passes.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only bash launcher harness; native Windows has no /bin/bash",
)

REPO = Path(__file__).resolve().parent.parent
LAUNCHER = REPO / "hooks" / "python-launcher.sh"


def _extract_func(name: str, source: str) -> str:
    """Pull a top-level `name() { ... }` bash function body out of the launcher."""
    m = re.search(rf"^{re.escape(name)}\(\) \{{$", source, re.MULTILINE)
    assert m, f"function {name}() not found in launcher"
    start = m.start()
    # Match to the first line that is exactly "}" at column 0 after start.
    rest = source[start:]
    end = re.search(r"^\}$", rest, re.MULTILINE)
    assert end, f"closing brace for {name}() not found"
    return rest[: end.end()]


def _probe_source() -> str:
    src = LAUNCHER.read_text(encoding="utf-8")
    return _extract_func("_probe_windowsapps_candidate", src)


def _write_fake(path: Path, *, mode: str) -> Path:
    """A fake WindowsApps candidate 'binary'. Called by the probe as
    `<bin> -c '<code>' <marker_path>` (Tier 1) and `<bin> --version` (Tier 2).

    Modes model the real response shapes the two-tier probe must discriminate:
    - "live_marker":  a normal live interpreter -- writes 'ok' on -c, and prints a
      real "Python X.Y.Z" on --version.
    - "live_no_marker": a LIVE interpreter that cannot write the marker (AV/DLP
      blocks the temp write, cygpath mismatch, cold-start over budget) but still
      prints "Python X.Y.Z" on --version. Tier 2 must accept it.
    - "dead": the Store redirector -- ignores -c (no marker), and on --version
      EXITS 0 (the historic false pass) but prints a localized "not found" message
      with NO version number. Tier 2 must reject it on output, not exit code.
    """
    lines = ["#!/bin/sh"]
    if mode in ("live_marker", "live_no_marker"):
        if mode == "live_marker":
            lines.append('if [ "$1" = "-c" ]; then printf ok > "$3"; exit 0; fi')
        else:
            lines.append('if [ "$1" = "-c" ]; then exit 0; fi')  # live, but no marker written
        lines.append('if [ "$1" = "--version" ]; then echo "Python 3.11.0"; exit 0; fi')
    else:  # dead Store redirector
        lines.append('if [ "$1" = "-c" ]; then exit 0; fi')
        lines.append('if [ "$1" = "--version" ]; then echo "Python was not found; install from the Store"; exit 0; fi')
    lines.append("exit 0")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _run_probe(candidate: Path, *, break_mktemp: bool = False) -> int:
    pre = "mktemp() { return 1; }\n" if break_mktemp else ""
    driver = (
        "set -u\n"
        + _probe_source()
        + "\n"
        # Override the safe-prefix gate so the probe logic (not path policy) is
        # what this test exercises.
        + "_is_safe_prefix() { return 0; }\n"
        # Shim `timeout` (absent on macOS) so `command -v timeout` passes and the
        # probe's fail-closed no-timeout guard does not short-circuit the test. The
        # shim strips `--kill-after=1s 2s` and runs the command unbounded (fakes
        # exit immediately, so no real timeout is needed).
        + 'timeout() { shift 2; "$@"; }\n'
        + pre
        + f'_probe_windowsapps_candidate "{candidate}"\n'
        + 'echo "RC=$?"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", driver], capture_output=True, text=True, timeout=30,
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    m = re.search(r"RC=(\d+)", proc.stdout)
    assert m, f"driver did not report RC; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return int(m.group(1))


def test_live_candidate_is_accepted(tmp_path):
    bin_ = _write_fake(tmp_path / "python3.exe", mode="live_marker")
    assert _run_probe(bin_) == 0, "a live candidate that writes the marker must be accepted"


def test_live_candidate_without_marker_accepted_via_version(tmp_path):
    """P1 (adversarial): a LIVE interpreter that cannot write the marker (blocked
    temp, cygpath mismatch, cold start) must still be accepted via its --version
    string -- NOT rejected. The pre-fix `ran=1 -> return 1` wrongly rejected it,
    breaking every hook when it was the only Python in PATH."""
    bin_ = _write_fake(tmp_path / "python3.exe", mode="live_no_marker")
    assert _run_probe(bin_) == 0, (
        "a live interpreter that only proves itself via --version must be accepted"
    )


def test_dead_stub_rejected_even_when_marker_probe_cannot_run(tmp_path):
    """P2 (adversarial): with no writable temp (mktemp fails), Tier 1 cannot run,
    so a dead stub reaches the --version path. It must still be REJECTED on the
    output (no version string), never accepted on the bare exit-0 the stub returns."""
    bin_ = _write_fake(tmp_path / "python3.exe", mode="dead")
    assert _run_probe(bin_, break_mktemp=True) != 0, (
        "a dead stub reached via the no-temp path must be rejected on --version "
        "output; a passing rc means the exit-code false-pass survived"
    )


def test_dead_stub_is_rejected(tmp_path):
    """The core guarantee: a dead stub (no marker on -c, no version on
    --version) is rejected on every path."""
    bin_ = _write_fake(tmp_path / "python3.exe", mode="dead")
    assert _run_probe(bin_) != 0, (
        "a dead Store stub must be rejected; a passing rc means it false-passed "
        "(regression)"
    )
