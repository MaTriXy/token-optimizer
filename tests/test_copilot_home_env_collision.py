#!/usr/bin/env python3
"""Regression tests for the COPILOT_HOME env collision fix (issue #78, round 2).

Root cause: ``COPILOT_HOME`` is GitHub Copilot CLI's OWN configuration
variable — setting it (e.g. to a WSL ``/mnt`` path for Token Optimizer's
benefit) is also read by the native-Windows Copilot CLI and relocates its own
session logging into a nonexistent path, so Copilot silently stops logging.

The fix gives Token Optimizer its own collision-free override
(``TOKEN_OPTIMIZER_COPILOT_HOME``), auto-detects the Windows profile under
``/mnt/c/Users/*`` when running as WSL-root (so no env var is needed), warns
when a ``/mnt`` ``COPILOT_HOME`` is present, and dedups repeated warnings.

Covered:
- TOKEN_OPTIMIZER_COPILOT_HOME takes precedence over COPILOT_HOME.
- A /mnt COPILOT_HOME earns the guardrail warning, exactly once (dedup).
- Non-WSL /mnt COPILOT_HOME falls back to default with one "rejected" warning.
- WSL-root auto-detect: single profile used; Public/system profiles skipped.
- WSL-root auto-detect: multiple profiles -> ambiguous -> default fallback.
- Auto-detect never fires outside the WSL-root context.
- TOKEN_OPTIMIZER_COPILOT_HOME implies the copilot runtime in detection.

Run directly:  python3 -m pytest tests/test_copilot_home_env_collision.py
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import runtime_env  # noqa: E402

_CONTROLLED_ENV = (
    "COPILOT_HOME",
    "TOKEN_OPTIMIZER_COPILOT_HOME",
    "TOKEN_OPTIMIZER_CURSOR_HOME",
    "CURSOR_PROJECT_DIR",
    "CURSOR_VERSION",
    "CODEX_HOME",
    "HERMES_HOME",
    "TOKEN_OPTIMIZER_RUNTIME",
)


def _run(env=None, *, wsl_root=False, mnt_root=None, fn=None):
    """Run ``fn(mnt_root)`` under a controlled env + faked WSL-root context.

    ``wsl_root`` stands in for the live ``/proc`` + euid detection so the tests
    are deterministic on any host (macOS can't read /proc or create /mnt/c/...).
    Warnings are reset so per-call dedup assertions are isolated.
    """
    env = env or {}
    saved_env = {k: os.environ.get(k) for k in _CONTROLLED_ENV}
    saved_wsl = runtime_env._is_wsl_context
    saved_root = runtime_env._wsl_root_context
    saved_warned = set(runtime_env._warned_messages)
    try:
        for k in _CONTROLLED_ENV:
            os.environ.pop(k, None)
        for k, v in env.items():
            if v is not None:
                os.environ[k] = v
        runtime_env._warned_messages.clear()
        runtime_env._is_wsl_context = lambda: bool(wsl_root)
        runtime_env._wsl_root_context = lambda: bool(wsl_root)
        return fn(mnt_root)
    finally:
        runtime_env._is_wsl_context = saved_wsl
        runtime_env._wsl_root_context = saved_root
        runtime_env._warned_messages.clear()
        runtime_env._warned_messages.update(saved_warned)
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _mk(base: Path, rel: str) -> Path:
    p = base / rel
    p.mkdir(parents=True, exist_ok=True)
    return p


# --- Part 1: TOKEN_OPTIMIZER_COPILOT_HOME precedence -----------------------

def test_to_var_takes_precedence_over_copilot_home():
    tmp = Path(tempfile.mkdtemp(prefix="t_tovar_"))
    mnt = _mk(tmp, "mnt")
    to_dir = _mk(mnt, "c/Users/me/.to_copilot")
    cp_dir = _mk(mnt, "c/Users/me/.copilot")

    def body(mnt_root):
        os.environ["TOKEN_OPTIMIZER_COPILOT_HOME"] = str(to_dir)
        os.environ["COPILOT_HOME"] = str(cp_dir)
        result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == to_dir.resolve(strict=False), result

    _run(wsl_root=True, mnt_root=mnt, fn=body)


# --- Part 4: /mnt COPILOT_HOME guardrail + dedup ---------------------------

@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only WSL /mnt path semantics")
def test_mnt_copilot_home_guardrail_fires_once():
    # The guardrail keys off the literal "/mnt/" prefix of the raw value (real
    # production path), independent of any mnt_root injection.
    def body(_mnt_root):
        os.environ["COPILOT_HOME"] = "/mnt/c/Users/me/.copilot"
        buf = io.StringIO()
        with redirect_stderr(buf):
            # Call several times: the doctor/install/hook paths each resolve.
            for _ in range(4):
                runtime_env.copilot_home()
        out = buf.getvalue()
        assert out.count("is a WSL /mnt path") == 1, repr(out)

    _run(wsl_root=True, fn=body)


def test_non_wsl_mnt_copilot_home_rejected_once_then_default():
    def body(_mnt_root):
        os.environ["COPILOT_HOME"] = "/mnt/c/Users/me/.copilot"
        buf = io.StringIO()
        with redirect_stderr(buf):
            for _ in range(4):
                result = runtime_env.copilot_home()
        out = buf.getvalue()
        assert result == Path.home() / ".copilot", result
        assert out.count("rejected (not a safe directory)") == 1, repr(out)

    _run(wsl_root=False, fn=body)


# --- Part 3: WSL-root auto-detect ------------------------------------------

def test_autodetect_single_profile():
    tmp = Path(tempfile.mkdtemp(prefix="t_auto1_"))
    prof = _mk(tmp, "c/Users/Assaf/.copilot")
    _mk(tmp, "c/Users/Public/.copilot")   # system profile, must be skipped

    def body(mnt_root):
        result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == prof.resolve(strict=False), result

    _run(wsl_root=True, mnt_root=tmp, fn=body)


def test_autodetect_multi_profile_falls_back_to_default():
    tmp = Path(tempfile.mkdtemp(prefix="t_auto2_"))
    _mk(tmp, "c/Users/Assaf/.copilot")
    _mk(tmp, "d/Users/Bob/.copilot")

    def body(mnt_root):
        result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == Path.home() / ".copilot", result

    _run(wsl_root=True, mnt_root=tmp, fn=body)


def test_autodetect_never_fires_outside_wsl_root():
    tmp = Path(tempfile.mkdtemp(prefix="t_auto3_"))
    _mk(tmp, "c/Users/Assaf/.copilot")

    def body(mnt_root):
        result = runtime_env.copilot_home(mnt_root=mnt_root)
        assert result == Path.home() / ".copilot", result

    _run(wsl_root=False, mnt_root=tmp, fn=body)


# --- Detection: TO var implies copilot runtime -----------------------------

def test_to_var_implies_copilot_runtime():
    def body(_mnt_root):
        os.environ["TOKEN_OPTIMIZER_COPILOT_HOME"] = "/mnt/c/Users/me/.copilot"
        assert runtime_env._copilot_signal() is True

    _run(wsl_root=False, fn=body)


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
