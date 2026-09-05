"""Cross-platform security and invalidation tests for python-launcher caching."""

from __future__ import annotations

import os
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
MIRROR = REPO / "plugins" / "token-optimizer" / "hooks" / "python-launcher.sh"


def _cache_file_for(tmp_path: Path, path_value: str, platform: str) -> str:
    source = LAUNCHER.read_text(encoding="utf-8")
    definitions = source[: source.index("\n_setup_interpreter_cache\n")]
    uname_bin = tmp_path / "bin"
    uname_bin.mkdir(exist_ok=True)
    uname = uname_bin / "uname"
    uname.write_text(f"#!/bin/sh\nprintf '%s\\n' '{platform}'\n", encoding="utf-8")
    uname.chmod(0o700)
    script = definitions + "\n_setup_interpreter_cache\nprintf '%s\\n' \"$_PY_CACHE_FILE\"\n"
    env = os.environ.copy()
    env["HOME"] = str(tmp_path / "home")
    env.pop("XDG_CACHE_HOME", None)
    env.pop("TOKEN_OPTIMIZER_PY_CACHE", None)
    env["PATH"] = f"{uname_bin}:{path_value}"
    result = subprocess.run(
        ["/bin/bash", "-c", script],
        cwd=str(LAUNCHER.parent),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_path_checksum_changes_cache_key(monkeypatch, tmp_path):
    first_path = f"{tmp_path}/venv-a/bin:/usr/bin:/bin"
    second_path = f"{tmp_path}/venv-b/bin:/usr/bin:/bin"

    first = _cache_file_for(tmp_path, first_path, "Linux")
    second = _cache_file_for(tmp_path, second_path, "Linux")

    assert first
    assert second
    assert first != second


def test_msys_cache_engages_inside_per_user_home(tmp_path):
    cache_file = _cache_file_for(tmp_path, "/usr/bin:/bin", "MINGW64_NT-10.0")

    assert "_cache_dir_is_per_user" in LAUNCHER.read_text(encoding="utf-8")
    assert cache_file
    assert Path(cache_file).parent == (
        tmp_path / "home" / ".cache" / "token-optimizer" / "pylauncher"
    )


def test_msys_ownership_fallback_keeps_posix_owner_check_and_confinement():
    source = LAUNCHER.read_text(encoding="utf-8")

    assert "[ -O \"$cache_dir\" ]" in source
    assert "MINGW*|MSYS*|CYGWIN*" in source
    assert "_cache_dir_is_per_user" in source
    assert 'cache_dir="/tmp/' not in source


def test_launcher_mirror_is_byte_identical():
    canonical = LAUNCHER.read_bytes()
    assert b"path_hash" in canonical
    assert canonical == MIRROR.read_bytes()


def test_cache_key_carries_probe_epoch(tmp_path):
    """poisoned-cache heal: the cache filename carries a probe-logic epoch, so
    a change to interpreter-liveness probing renames the key and every stale record
    is ignored once on upgrade. Without it, a user already bitten by the dead stub keeps a
    record naming the dead WindowsApps stub -- and the fixed probe runs only on a
    cache MISS, so on every cache HIT the dead stub is re-exec'd forever."""
    cache_file = _cache_file_for(tmp_path, "/usr/bin:/bin", "Linux")
    assert "/interpreter-e2-" in cache_file, (
        f"cache key must carry the probe-logic epoch (interpreter-e2-...); got {cache_file}"
    )
