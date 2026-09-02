#!/usr/bin/env python3
"""LeaseLock.acquire must never give up on an observably FREE lease.

THE FLAKE THIS PINS
-------------------
tests/test_settings_never_clobbered.py::test_cleanup_period_and_statusline_writers_merge_stale_reads
failed ~1-in-5 on clean main. Instrumented interleaving (2026-09-02, both
threads entering _write_settings_atomic simultaneously):

    iter statusLine   1 try_create True          <- wins the lease, writes ~1ms
    iter cleanupPeriodDays 1 try_create False     <- lease held
    reclaim cleanupPeriodDays False                <- not expired, not released yet
    acquire cleanupPeriodDays False 0.0777 iters=2 <- gave up

The contender requested time.sleep(min(remaining, 0.004..0.012)) but the OS
delivered ~76ms (loaded box), so on loop iteration 2 the top-of-loop deadline
check ``monotonic >= stop`` fired BEFORE the retry attempt and acquire
returned False -- while the lease had been free for ~70ms. The bottom-of-loop
check already bounds the wait; the top check only ever skipped a final
attempt on a free lock.

THE FIX THIS PINS
-----------------
acquire() attempts first and checks the deadline after a failed attempt, so a
waiter that wakes late (overslept sleep, descheduled process) still takes a
free lease instead of reporting denial. The wait bound is unchanged: the loop
still exits at ``stop`` after a failed attempt, and never sleeps past it.
"""

from __future__ import annotations

import importlib
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys_path_prepend = str(SCRIPTS)
if sys_path_prepend not in __import__("sys").path:
    __import__("sys").path.insert(0, sys_path_prepend)

hook_runtime = importlib.import_module("hook_runtime")
LeaseLock = hook_runtime.LeaseLock


def _wait_for_lease_file(path: Path, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.001)
    raise AssertionError("holder never created the lease file")


def test_acquire_takes_a_freed_lease_after_an_overslept_wait(tmp_path, monkeypatch):
    """Deterministic replay of the flake: the contender's sleep oversleeps its
    whole deadline, the holder releases mid-sleep, and the contender must still
    acquire the (tombstoned, released, same-pid) lease instead of returning
    False on a free lock."""
    lease_path = tmp_path / "settings.lease"
    holder = LeaseLock(lease_path, acquire_timeout=0.0)
    assert holder.acquire() is True
    _wait_for_lease_file(lease_path)

    real_sleep = time.sleep

    def oversleeping_sleep(seconds):
        # Simulate a loaded box: a <=12ms budgeted sleep delivered ~10x late,
        # past the contender's entire acquire deadline.
        real_sleep(0.25)

    class _ShimTime:
        """Delegate everything to time except sleep, which oversleeps."""

        def __getattr__(self, name):
            return getattr(time, name)

        @staticmethod
        def sleep(seconds):
            oversleeping_sleep(seconds)

    monkeypatch.setattr(hook_runtime, "time", _ShimTime())

    # Guard against a vacuous pass: the contender's FIRST attempt must race
    # the still-held lease (return False). If scheduling ever delayed the
    # contender past the 50ms release, it would win on iteration 1 and never
    # exercise the oversleep path this test exists to pin.
    attempts = []
    orig_try_create = LeaseLock._try_create

    def spying_try_create(self):
        result = orig_try_create(self)
        attempts.append(result)
        return result

    monkeypatch.setattr(LeaseLock, "_try_create", spying_try_create)

    releaser = threading.Timer(0.05, holder.release)
    releaser.start()
    contender = LeaseLock(lease_path, acquire_timeout=0.075)
    try:
        assert contender.acquire() is True, (
            "acquire() gave up on a lease that was released while the waiter "
            "overslept its deadline; the top-of-loop deadline check skipped "
            "the final free attempt"
        )
        assert attempts[0] is False, (
            "the contender's first attempt did not race the held lease; this "
            "run exercised the iteration-1 win, not the oversleep replay"
        )
    finally:
        releaser.join()
        if contender.acquired:
            contender.release()


def test_acquire_still_respects_the_deadline_under_real_contention(tmp_path):
    """The fix must not unbound the wait: a lease that stays HELD past the
    deadline still yields False (guard the guard)."""
    lease_path = tmp_path / "settings.lease"
    holder = LeaseLock(lease_path, acquire_timeout=0.0)
    assert holder.acquire() is True
    try:
        contender = LeaseLock(lease_path, acquire_timeout=0.05)
        t0 = time.monotonic()
        assert contender.acquire() is False
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0, f"acquire blocked {elapsed:.2f}s, deadline not respected"
    finally:
        holder.release()
