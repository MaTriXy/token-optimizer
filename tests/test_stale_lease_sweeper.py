"""Bounded stale-lease sweeper.

``hook_runtime.LeaseLock.release()`` leaves the canonical ``.qlease`` tombstone
on disk for the next contender to reclaim, and a ``_HookTimeout`` between acquire
and release can also leak the per-nonce candidate. Sessions are one-shot, so most
tombstones get no future contender and leak forever. ``_sweep_stale_leases`` is
the self-healing backstop: it removes ONLY lease artifacts whose expiry is
demonstrably long in the past, so no live locker can ever be touched.

These tests prove:
  * an expired ``.qlease`` (``expires_wall`` in the past) is removed;
  * a day-old ``.candidate-*`` / ``.reclaim-*`` / legacy ``.qlock`` is removed;
  * a fresh/live ``.qlease`` (``expires_wall`` in the future) is NOT removed;
  * a recent ``.candidate-*`` is NOT removed (live acquisition in flight);
  * a corrupt/partial ``.qlease`` never raises and is removed only past 24h mtime;
  * the scan is capped at ``max_files``;
  * the measure-side ``maybe_sweep_stale_leases`` throttle runs once then skips.

Run: python3 -m pytest tests/test_stale_lease_sweeper.py -v
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hook_runtime  # noqa: E402


def _qlease(path: Path, expires_wall: float, released: int = 0) -> Path:
    """Write a minimal valid ``.qlease`` metadata file."""
    path.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "nonce": "deadbeef" * 4,
                "released": released,
                "reuse_wall": expires_wall,
                "created_wall": expires_wall - 10,
                "expires_wall": expires_wall,
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _age(path: Path, seconds_old: float) -> None:
    ts = time.time() - seconds_old
    os.utime(path, (ts, ts))


def test_expired_qlease_is_removed(tmp_path):
    now = time.time()
    stale = _qlease(tmp_path / "quality-cache-dead.qlease", expires_wall=now - 7200)
    removed = hook_runtime._sweep_stale_leases(tmp_path, now=now)
    assert not stale.exists(), "expired .qlease survived the sweep"
    assert removed >= 1


def test_live_qlease_is_not_removed(tmp_path):
    now = time.time()
    live = _qlease(tmp_path / "quality-cache-live.qlease", expires_wall=now + 60)
    removed = hook_runtime._sweep_stale_leases(tmp_path, now=now)
    assert live.exists(), "live .qlease was reaped (false positive on a live locker)"
    assert removed == 0


def test_day_old_candidate_and_qlock_removed(tmp_path):
    now = time.time()
    old_candidate = tmp_path / ".quality-cache-x.candidate-abc"
    old_candidate.write_bytes(b"orphan")
    _age(old_candidate, 86400 + 3600)

    old_reclaim = tmp_path / ".quality-cache-x.reclaim-dead"
    old_reclaim.write_bytes(b"orphan")
    _age(old_reclaim, 86400 + 3600)

    old_qlock = tmp_path / "quality-cache-y.qlock"
    old_qlock.write_bytes(b"legacy")
    _age(old_qlock, 86400 + 3600)

    hook_runtime._sweep_stale_leases(tmp_path, now=now)
    assert not old_candidate.exists(), "day-old candidate survived"
    assert not old_reclaim.exists(), "day-old reclaim survived"
    assert not old_qlock.exists(), "day-old legacy .qlock survived"


def test_recent_candidate_is_not_removed(tmp_path):
    now = time.time()
    young = tmp_path / ".quality-cache-live.candidate-fresh"
    young.write_bytes(b"live-acquirer")
    # just-created mtime
    hook_runtime._sweep_stale_leases(tmp_path, now=now)
    assert young.exists(), "recent live candidate was reaped (false positive)"


def test_corrupt_qlease_never_raises_and_only_removed_past_24h(tmp_path):
    now = time.time()
    corrupt_fresh = tmp_path / "quality-cache-fresh-corrupt.qlease"
    corrupt_fresh.write_bytes(b"{not even json")
    corrupt_old = tmp_path / "quality-cache-old-corrupt.qlease"
    corrupt_old.write_bytes(b"\x00\x01partial")
    _age(corrupt_old, 86400 + 3600)

    # Must not raise on either file.
    removed = hook_runtime._sweep_stale_leases(tmp_path, now=now)
    # Fresh corrupt file is left alone (could be mid-publication).
    assert corrupt_fresh.exists(), "fresh corrupt .qlease was removed too eagerly"
    # Old corrupt file is reaped past the 24h mtime gate.
    assert not corrupt_old.exists(), "old corrupt .qlease survived"
    assert removed >= 1


def test_sweep_caps_at_max_files(tmp_path):
    now = time.time()
    # Create 10 stale qlock files; cap the scan at 3 entries.
    for i in range(10):
        p = tmp_path / f"quality-cache-{i}.qlock"
        p.write_bytes(b"legacy")
        _age(p, 86400 + 3600)
    removed = hook_runtime._sweep_stale_leases(tmp_path, now=now, max_files=3)
    # At most 3 files inspected -> at most 3 removed; the rest survive this run.
    assert removed <= 3
    survivors = list(tmp_path.glob("*.qlock"))
    assert len(survivors) >= 7, "max_files cap was not honored"


def test_sweep_missing_dir_is_noop(tmp_path):
    assert hook_runtime._sweep_stale_leases(tmp_path / "does-not-exist") == 0


def test_sweep_skips_subdirectories(tmp_path):
    sub = tmp_path / "subdir"
    sub.mkdir()
    (sub / "quality-cache-dead.qlease").write_text("x")
    # A directory named like a lease must never be unlinked.
    hook_runtime._sweep_stale_leases(tmp_path, now=time.time() + 9999)
    assert sub.exists()


# --- measure-side throttle wrapper -----------------------------------------


def _load_measure(monkeypatch, tmp_path):
    """Import measure with an isolated QUALITY_CACHE_DIR + sweep marker."""
    import importlib

    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    cache_dir = tmp_path / "to-cache"
    cache_dir.mkdir()
    monkeypatch.setattr(mod, "QUALITY_CACHE_DIR", cache_dir)
    monkeypatch.setattr(
        mod, "_QLEASE_SWEEP_MARKER", cache_dir / ".qlease-sweep-throttle"
    )
    return mod


def test_maybe_sweep_throttles_to_once_per_day(monkeypatch, tmp_path):
    mod = _load_measure(monkeypatch, tmp_path)
    now = time.time()
    stale = mod.QUALITY_CACHE_DIR / "quality-cache-dead.qlease"
    stale.write_text(
        json.dumps(
            {
                "pid": 1,
                "nonce": "n",
                "released": 0,
                "reuse_wall": now - 7200,
                "created_wall": now - 7210,
                "expires_wall": now - 7200,
            }
        )
    )
    # First run sweeps.
    first = mod.maybe_sweep_stale_leases()
    assert first >= 1
    assert not stale.exists()
    # Re-create a stale file; the second call within 24h must be throttled out.
    stale.write_text(
        json.dumps(
            {
                "pid": 1,
                "nonce": "n",
                "released": 0,
                "reuse_wall": now - 7200,
                "created_wall": now - 7210,
                "expires_wall": now - 7200,
            }
        )
    )
    second = mod.maybe_sweep_stale_leases()
    assert second == 0, "sweeper ran twice inside the 24h throttle window"
    assert stale.exists(), "throttled run must not touch the dir"


def test_maybe_sweep_never_raises_on_sweeper_error(monkeypatch, tmp_path):
    mod = _load_measure(monkeypatch, tmp_path)

    def boom(_dir):
        raise RuntimeError("sweeper explosion")

    monkeypatch.setattr(mod, "_sweep_stale_leases", boom)
    # Must swallow and return 0, never raise into a hook.
    assert mod.maybe_sweep_stale_leases() == 0
