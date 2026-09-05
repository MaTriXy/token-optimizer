"""Mechanism test: the 20s HookDeadline closes the hook stdout pipe.

The Windows hang: a fossilized SessionEnd hook runs the raw shape
``python measure.py collect --quiet && python measure.py dashboard --quiet``
directly from settings.json. When the host TerminateProcess-es the hook
runner, the measure.py grandchild can keep the inherited stdout pipe open
forever, wedging the turn at 3/4. The fix arms a 20s ``HookDeadline`` inside
``_dispatch_collect`` / ``_dispatch_dashboard`` on the hook path; the watchdog
daemon thread calls ``os._exit(0)`` when the budget elapses, which closes the
pipe regardless of what collect was doing.

This is the POSIX proxy for that Windows hang. The previous version of this
test was VACUOUS: it ran ``collect`` against an EMPTY tmp HOME, so collect
finished in ~0.1s on its own and the pipe EOF'd from normal completion, it
passed with every fix layer removed. This version drives a collect that
genuinely BLOCKS past the 20s budget, invokes the RAW fossil command shape
(``python measure.py collect --quiet`` — NOT run.py, which the fossil never
goes through), and asserts the pipe EOFs in a tight window around the 20s
budget, proving the ``HookDeadline`` ``os._exit(0)`` — not normal completion
— closed it.

How the block is made deterministic and machine-independent: a FIFO is seeded
as a session transcript under ``$CLAUDE_CONFIG_DIR/projects/<proj>/*.jsonl``.
``_parse_session_jsonl`` opens it with a blocking ``open(path, "r")``; with no
writer ever connecting, the open() blocks FOREVER. So the only thing that can
close the stdout pipe is the 20s ``HookDeadline`` — there is no "normal
completion" path that could EOF it. This removes the machine-speed variance
that would come from seeding enough real transcript data to exceed 20s.

Discrimination (fix-present vs fix-absent) is provided by a companion control
that runs the SAME blocking collect with ``--rebuild``. ``--rebuild`` is the
one code path where the 20s budget is genuinely NOT armed
(``_install_hook_budget(20) if (_running_under_hook() and not rebuild) else
None``), so the control proves that without the budget the read would block
past the window (the FIFO never EOFs). Combined with the tight ~20s EOF in the
budget-armed run, the deadline is the only possible cause of the pipe closing.

Not skipped on Linux/macOS: this IS the POSIX proxy for the Windows
TerminateProcess hang (SIGKILL is not needed here; the deadline self-exits the
process, which is the exact mechanism that saves the inherited pipe). Only
skipped on native Windows, where mkfifo is unavailable.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
MEASURE_PY = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

# The 20s dispatch budget armed by _dispatch_collect on the hook path. The
# deadline fires os._exit(0) ~20s after _install_hook_budget(20) is called,
# which itself runs after measure.py's import/setup (~0.4-1s). The lower bound
# proves the collect did not finish instantly (the vacuous case); the upper
# bound proves the deadline fired well before any "no budget" run could be
# confused with it (the no-budget control never EOFs at all).
EOF_LOWER = 18
EOF_UPPER = 26

# The no-budget control probe. Must exceed the 20s deadline by enough to prove
# "without the budget, the read blocks past the deadline window". The FIFO has
# no writer, so a budget-less run blocks forever; this probe only needs to pass
# the 20s mark to be conclusive.
NO_BUDGET_PROBE = 23


def _read_until_eof_or_timeout(proc: subprocess.Popen, timeout: float):
    """Read stdout until EOF or ``timeout`` elapses.

    Returns ``(data, elapsed, eof)``. Uses select so a regression (deadline
    never fires) fails the assertion instead of hanging pytest.
    """
    start = time.monotonic()
    chunks: list[bytes] = []
    eof = False
    fd = proc.stdout.fileno()
    while True:
        remaining = timeout - (time.monotonic() - start)
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        chunk = os.read(fd, 65536)
        if not chunk:
            eof = True
            break
        chunks.append(chunk)
    elapsed = time.monotonic() - start
    return b"".join(chunks), elapsed, eof


def _blocking_collect_env(tmp_path: Path) -> dict:
    """Build an isolated env whose collect blocks forever on a FIFO transcript.

    ``CLAUDE_CONFIG_DIR`` redirects claude_home() (and thus the projects/ scan
    and settings.json) to a tmp dir. ``TOKEN_OPTIMIZER_SNAPSHOT_DIR`` isolates
    the trends DB. ``TOKEN_OPTIMIZER_RUNTIME=claude`` pins the runtime so no
    Codex/Hermes/Copilot adapter intercepts collect_sessions. The FIFO under
    projects/<proj>/*.jsonl makes _parse_session_jsonl block at open() with no
    writer, so collect never reaches normal completion.
    """
    cfg = tmp_path / "claude"
    cfg.mkdir()
    proj = cfg / "projects" / "testproj"
    proj.mkdir(parents=True)
    fifo = proj / "block.jsonl"
    os.mkfifo(fifo)

    env = {
        "HOME": str(tmp_path / "home"),
        "CLAUDE_CONFIG_DIR": str(cfg),
        "TOKEN_OPTIMIZER_SNAPSHOT_DIR": str(tmp_path / "snap"),
        "TOKEN_OPTIMIZER_RUNTIME": "claude",
        "TOKEN_OPTIMIZER_NO_PROC_SCAN": "1",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUTF8": "1",
    }
    (tmp_path / "home").mkdir(exist_ok=True)
    return env


def _launch_collect(env: dict, *extra_args: str) -> subprocess.Popen:
    """Launch the RAW fossil command shape: ``python measure.py collect ...``.

    stdin is DEVNULL so ``_running_under_hook()`` sees a non-tty stdin and arms
    the 20s HookDeadline (the fossil invokes measure.py directly, with no
    --hook flag and no TOKEN_OPTIMIZER_HOOK env marker, exactly like this).
    """
    return subprocess.Popen(
        [sys.executable, str(MEASURE_PY), "collect", "--quiet", *extra_args],
        cwd=str(REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO proxy; Windows uses TerminateProcess")
def test_collect_deadline_closes_stdout_pipe(tmp_path):
    """The 20s HookDeadline os._exit(0) closes the pipe on a blocking collect.

    Drives a collect that blocks forever (FIFO transcript), invokes the raw
    ``python measure.py collect --quiet`` fossil shape, and asserts stdout
    EOFs in a tight window around the 20s budget. Because the FIFO makes
    normal completion impossible, the ONLY thing that can close the pipe is
    the HookDeadline, so an EOF in 18<elapsed<26 proves the deadline fired.
    """
    env = _blocking_collect_env(tmp_path)
    proc = _launch_collect(env)
    try:
        data, elapsed, eof = _read_until_eof_or_timeout(proc, timeout=EOF_UPPER + 4)
        assert eof, (
            f"stdout pipe did NOT EOF within {EOF_UPPER + 4:.0f}s of a blocking "
            f"collect (elapsed={elapsed:.1f}s). The 20s HookDeadline never fired "
            f"os._exit(0) -- the fix is absent or regressed. stderr="
            f"{proc.stderr.read().decode(errors='replace')[:300]!r}"
        )
        assert EOF_LOWER < elapsed < EOF_UPPER, (
            f"stdout EOF'd at {elapsed:.2f}s, outside the tight 20s-deadline "
            f"window ({EOF_LOWER}<{elapsed:.2f}<{EOF_UPPER}). A sub-{EOF_LOWER}s "
            f"EOF means collect did not actually block (vacuous); a >{EOF_UPPER}s "
            f"EOF means the deadline fired late or not at all. stderr="
            f"{proc.stderr.read().decode(errors='replace')[:300]!r}"
        )
        # The deadline exits 0 (os._exit(0)); a non-zero exit would mean
        # something else closed the pipe.
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
        assert rc == 0, f"deadline-closed collect exited {rc}, expected 0 (os._exit(0))"
        # --quiet collect writes nothing to stdout; any data would mean the
        # fossil printed before blocking, which is fine but should be empty.
        assert data == b"" or b"Token Optimizer" not in data
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        for s in (proc.stdout, proc.stderr):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX FIFO proxy; Windows uses TerminateProcess")
def test_collect_without_budget_hangs_past_window(tmp_path):
    """Discrimination control: WITHOUT the 20s budget, the blocking collect
    does NOT EOF within the deadline window.

    Runs the same FIFO-blocked collect with ``--rebuild``, which is the one
    code path where the budget is genuinely NOT armed
    (``_install_hook_budget(20) if (_running_under_hook() and not rebuild)
    else None``). The FIFO has no writer, so a budget-less collect blocks
    forever and the pipe never EOFs. Probing past the 20s deadline and seeing
    no EOF proves the EOF in the budget-armed companion is caused by the
    HookDeadline, not by normal completion or any other mechanism.
    """
    env = _blocking_collect_env(tmp_path)
    proc = _launch_collect(env, "--rebuild")
    try:
        _data, elapsed, eof = _read_until_eof_or_timeout(proc, timeout=NO_BUDGET_PROBE)
        assert not eof, (
            f"stdout EOF'd at {elapsed:.2f}s on a budget-LESS blocking collect "
            f"(--rebuild disables the 20s HookDeadline). The FIFO has no writer, "
            f"so collect should block forever -- an EOF here means something OTHER "
            f"than the deadline closed the pipe, which would invalidate the "
            f"budget-armed test's discrimination. stderr="
            f"{proc.stderr.read().decode(errors='replace')[:300]!r}"
        )
        assert elapsed >= NO_BUDGET_PROBE - 0.5, (
            f"control probe returned early at {elapsed:.2f}s without EOF; expected "
            f"to wait the full {NO_BUDGET_PROBE}s. stdout pipe should stay open."
        )
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        for s in (proc.stdout, proc.stderr):
            if s:
                try:
                    s.close()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Windows-native proof. The tests above block collect with a FIFO and use
# SIGKILL -> both POSIX-only (skipped on win32). This one exercises the exact
# load-bearing mechanism -- HookDeadline's os._exit(0) releasing an
# inherited stdout pipe when a hook process stalls -- with NO mkfifo and NO
# SIGKILL, so it runs on NATIVE WINDOWS in CI. It is the real-Windows evidence
# that a wedged, budget-bounded hook can never hold the host's pipe open.
# ---------------------------------------------------------------------------

_SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
_DEADLINE_SECONDS = 2.0


def test_hookdeadline_closes_inherited_stdout_pipe_crossplatform():
    """A child that arms HookDeadline(2) and then blocks forever must have its
    inherited stdout pipe EOF at ~2s (the deadline's os._exit(0)) on EVERY OS,
    Windows included. Proves the pipe-closure mechanism natively on Windows
    where the FIFO/SIGKILL harness above cannot run."""
    child = (
        "import sys, time\n"
        f"sys.path.insert(0, r'{_SCRIPTS}')\n"
        "from hook_runtime import HookDeadline\n"
        "HookDeadline(%r).start()\n" % _DEADLINE_SECONDS
        + "time.sleep(600)\n"  # block far past the deadline; only os._exit ends it
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,  # the diagnostic goes to fd 2; we watch stdout EOF
    )
    start = time.monotonic()
    try:
        data = proc.stdout.read()  # blocks until the child's stdout pipe EOFs
        elapsed = time.monotonic() - start
    finally:
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError(
                "child did not exit after its stdout EOF'd; HookDeadline os._exit "
                "did not terminate the process"
            )
    # EOF must arrive from the deadline firing (~2s), not instantly (which would
    # mean the child exited on its own, proving nothing) and not unbounded.
    assert data == b"", f"expected empty stdout (child writes nothing), got {data!r}"
    assert _DEADLINE_SECONDS - 0.5 < elapsed < _DEADLINE_SECONDS + 6.0, (
        f"stdout pipe EOF'd at {elapsed:.2f}s, outside the HookDeadline window "
        f"(~{_DEADLINE_SECONDS}s). A sub-{_DEADLINE_SECONDS}s EOF means the child "
        f"exited without the deadline; a >{_DEADLINE_SECONDS + 6:.0f}s EOF means "
        "os._exit never fired and the pipe was held open (the wedge)."
    )
    assert proc.returncode == 0, f"HookDeadline must os._exit(0); got {proc.returncode}"
