#!/usr/bin/env python3
"""Regression tests for PR #166 (hook I/O and runtime resource hardening).

Each test reproduces a bug that was demonstrated live on main before #166
landed (evidence in the PR description and sessions/2026-09-01-parallel-sprint/
reports/A-PR-166-verdict.md):

  1. read_stdin_hook_input blocked past its own _STDIN_TIMEOUT on a partial
     payload with a held pipe (select guarantees one byte; the old
     read(max_bytes) blocked until max_bytes or EOF).
  2. A planted reader-less FIFO at module_runner's once-a-day ro-pyc marker
     path hung the hook indefinitely (open(marker, "w") on a FIFO blocks;
     the except cannot interrupt a blocked syscall).
  3. _write_dashboard_meta_atomic leaked tmp_fd when os.fchmod raised between
     mkstemp and fdopen (50 leaked fds in 50 forced-failure calls).
  4. _handler_deadline restored an already-expired outer timer via
     setitimer(0.0), which DISARMS it, silently dropping the outer budget for
     every later handler in the runner.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
HOOKS = REPO / "hooks"


def _load_module(path: Path, name: str):
    # measure.py imports hook_io from hooks/; other measure-loading tests do
    # the same path prepends.
    for p in (str(SCRIPTS), str(HOOKS)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1. stdin partial payload must not hang past the deadline
# ---------------------------------------------------------------------------

def test_stdin_partial_payload_returns_within_deadline(tmp_path):
    """A host that writes a partial JSON payload and holds the pipe open must
    not hang the hook: the read is bounded by _STDIN_TIMEOUT end to end."""
    code = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(SCRIPTS)!r})\n"
        "import hook_io\n"
        "t0 = time.time()\n"
        "hook_io.read_stdin_hook_input(max_bytes=1_048_576)\n"
        "print('returned after %.2fs' % (time.time() - t0), flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    proc.stdin.write('{"partial": tru')  # valid-ish start, never completed
    proc.stdin.flush()
    try:
        out, _err = proc.communicate(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        pytest.fail("read_stdin_hook_input hung past 8s on a partial payload "
                    "with the pipe held open (deadline not enforced end to end)")
    assert "returned" in out, f"child crashed instead of timing out: {out!r}"


# --------------------------------------------------------------------------- #
# 2. planted FIFO at the ro-pyc marker path must not hang the hook
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.mkfifo is POSIX-only; the planted-FIFO attack shape requires a "
    "filesystem with FIFO semantics.",
)
def test_planted_fifo_marker_does_not_hang_the_hook(tmp_path):
    """A reader-less FIFO planted at the marker path (backdated to defeat the
    once-a-day freshness check) must be refused, not block the hook."""
    module_runner = _load_module(HOOKS / "module_runner.py", "module_runner_regr")
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    scripts_dir.chmod(0o555)  # force the read-only-scripts-dir branch
    tag = hashlib.sha1(str(scripts_dir).encode("utf-8", "replace")).hexdigest()[:12]
    marker = Path(tempfile.gettempdir()) / f".token-optimizer-ro-pyc-{tag}"
    try:
        os.mkfifo(marker)
        old = time.time() - 172800
        os.utime(marker, (old, old))
        done = []
        thread = threading.Thread(
            target=lambda: (module_runner._warn_readonly_scripts_dir_once(str(scripts_dir)),
                            done.append(True)),
            daemon=True,
        )
        thread.start()
        thread.join(timeout=5)
        assert done, ("hook hung >=5s on a planted FIFO marker; the marker write "
                      "must be exclusive and non-blocking (fail-open)")
    finally:
        scripts_dir.chmod(0o755)
        try:
            marker.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 3. interrupted atomic write must not leak the mkstemp fd
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="os.fchmod is absent on Windows, so the leak window between mkstemp "
    "and fdopen cannot be forced there.",
)
def test_dashboard_meta_write_does_not_leak_fd_on_fchmod_failure(monkeypatch):
    """Forcing os.fchmod to raise between mkstemp and fdopen must not leak the
    descriptor: 50 failed writes used to leak 50 fds."""
    measure = _load_module(SCRIPTS / "measure.py", "measure_regr")
    meta_path = Path(tempfile.mkdtemp()) / "meta.json"

    def fd_count():
        return len(os.listdir("/dev/fd"))

    real_fchmod = os.fchmod

    def boom(*_args, **_kwargs):
        raise OSError("forced fchmod failure")

    monkeypatch.setattr(os, "fchmod", boom)
    before = fd_count()
    for _ in range(50):
        measure._write_dashboard_meta_atomic(meta_path)  # fail-soft, never raises
    after = fd_count()
    monkeypatch.setattr(os, "fchmod", real_fchmod)
    assert after - before == 0, (
        f"interrupted atomic writes leaked {after - before} fds "
        "(tmp_fd not closed when fchmod raises before fdopen owns it)"
    )


# --------------------------------------------------------------------------- #
# 4. an already-expired outer timer must be delivered, not disarmed
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="setitimer/SIGALRM are POSIX-only; the nested-budget timer restore "
    "under test does not exist on Windows.",
)
def test_expired_outer_budget_is_delivered_not_disarmed():
    """When the inner budget context exits after the outer deadline already
    elapsed, the outer budget must fire (via the restored handler), not be
    silently disarmed by setitimer(0.0)."""
    ptr = _load_module(HOOKS / "posttooluse_runner.py", "ptr_regr")

    events = []

    def outer_handler(_signum, _frame):
        events.append("outer-fired")
        raise ptr._HandlerBudgetExceeded(0.2)

    signal_module = __import__("signal")
    previous = signal_module.getsignal(signal_module.SIGALRM)
    signal_module.signal(signal_module.SIGALRM, outer_handler)
    signal_module.setitimer(signal_module.ITIMER_REAL, 0.2)
    try:
        try:
            with ptr._handler_deadline(5.0):
                time.sleep(0.5)  # outer expires mid-inner; inner masks the signal
            events.append("inner-exited-cleanly")
        except ptr._HandlerBudgetExceeded:
            events.append("budget-exceeded")
        remaining = signal_module.getitimer(signal_module.ITIMER_REAL)[0]
        # The outer budget must have been DELIVERED (its handler ran, raising
        # its own budget exception), not silently disarmed for later handlers.
        assert "outer-fired" in events, (
            f"expired outer budget was disarmed instead of delivered: {events}; "
            f"outer timer remaining after inner exit: {remaining:.3f}s"
        )
    finally:
        signal_module.setitimer(signal_module.ITIMER_REAL, 0)
        signal_module.signal(signal_module.SIGALRM, previous)
