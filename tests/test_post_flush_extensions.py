"""Post-flush extension loader: off by default, config-dir-only, fail-open,
budget passed through.

Run: python3 -m pytest tests/test_post_flush_extensions.py -v
"""
import importlib
import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"


@pytest.fixture()
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("TOKEN_OPTIMIZER_SNAPSHOT_DIR", str(tmp_path / "snap"))
    monkeypatch.setenv("TOKEN_OPTIMIZER_CONFIG_DIR", str(tmp_path / "config"))
    (tmp_path / "snap").mkdir()
    sys.path.insert(0, str(SCRIPTS))
    if "measure" in sys.modules:
        del sys.modules["measure"]
    mod = importlib.import_module("measure")
    importlib.reload(mod)
    mod.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(mod, "CLAUDE_DIR", tmp_path / "claude")
    yield mod
    if "measure" in sys.modules:
        del sys.modules["measure"]


def _install_ext(m, body):
    ext_dir = m.CONFIG_DIR / "extensions"
    ext_dir.mkdir(exist_ok=True)
    p = ext_dir / "post_flush.py"
    p.write_text(body)
    os.chmod(str(p), 0o600)
    return p


def test_off_by_default_no_io(m, monkeypatch):
    """M-9: the old test patched builtins.open (which the code never calls)
    and passed only because the file didn't exist — giving false confidence
    that the off-by-default path does no I/O. The code DOES call os.open as
    its existence check (returns ENOENT, no file), which is correct. The
    real invariant is that no code LOADING happens: compile/exec are never
    called when no extension file exists.
    """
    compile_calls = []

    real_compile = compile

    def spy_compile(*a, **k):
        compile_calls.append(1)
        return real_compile(*a, **k)

    monkeypatch.setattr("builtins.compile", spy_compile)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    assert not compile_calls, "compile() should not be called when no extension exists"


def test_loads_only_from_config_dir(m, tmp_path, monkeypatch):
    _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    # a decoy elsewhere must be ignored (no env override exists)
    decoy = tmp_path / "decoy" / "post_flush.py"
    decoy.parent.mkdir()
    decoy.write_text("def run(ctx):\n    raise AssertionError('decoy ran')")
    monkeypatch.setenv("TO_EXTENSIONS_DIR", str(decoy.parent))  # not honoured
    out = m._run_post_flush_extensions(time_left_fn=lambda: 10)
    assert out == "ran"


def test_context_keys(m):
    body = (
        "def run(ctx):\n"
        "    assert set(ctx) >= {'trends_db', 'snapshot_dir', 'config_dir',"
        " 'runtime', 'version', 'time_left_fn'}\n"
        "    return ctx['time_left_fn']()\n")
    _install_ext(m, body)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 7, version="5.13.4") == 7


def test_fail_open_on_extension_exception(m):
    _install_ext(m, "def run(ctx):\n    raise RuntimeError('exploded')\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_fail_open_on_broken_module(m):
    _install_ext(m, "this is not python (((\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_fail_open_on_missing_run(m):
    _install_ext(m, "x = 1\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_fail_open_on_non_callable_run(m):
    _install_ext(m, "run = 'not a function'\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the group/world-writable mode-bit guard is POSIX-only (Windows "
    "fstat reports 0o666 for every writable file); on Windows the config "
    "directory's own ACLs are the protection, per docs/local-extensions.md.",
)
def test_world_writable_extension_ignored(m):
    p = _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    os.chmod(str(p), 0o666)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the group/world-writable mode-bit guard is POSIX-only (Windows "
    "fstat reports 0o666 for every writable file); on Windows the config "
    "directory's own ACLs are the protection, per docs/local-extensions.md.",
)
def test_group_writable_extension_ignored(m):
    """M-10: the code checks S_IWGRP | S_IWOTH but the old test only exercised
    S_IWOTH (0o666). A group-writable-but-not-world-writable file (0o660) was
    never tested and could regress undetected.
    """
    p = _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    os.chmod(str(p), 0o660)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_worker_completes_and_releases_lock_with_extension(m, monkeypatch):
    _install_ext(m, "def run(ctx):\n    raise RuntimeError('boom')\n")
    m._run_session_end_flush_worker([])
    assert m._acquire_session_end_flush_lock() is not None  # lock released
    m._release_session_end_flush_lock(m._acquire_session_end_flush_lock())


def test_worker_runs_extension_after_flush(m):
    _install_ext(m, "def run(ctx):\n"
                    "    import json, pathlib\n"
                    "    p = pathlib.Path(ctx['snapshot_dir']) / 'ext-marker'\n"
                    "    p.write_text(ctx['version'])\n"
                    "    return True\n")
    m._run_session_end_flush_worker([])
    assert (m.SNAPSHOT_DIR / "ext-marker").read_text() == m.TOKEN_OPTIMIZER_VERSION


# ---------------------------------------------------------------------------
# C-5 / H-5 / H-6 / M-4: extension loader hardening.
#
# The loader had weaker fd hygiene than its sibling marker writer
# (module_runner.py): no O_NOFOLLOW (symlinks followed), no O_NONBLOCK (FIFOs
# block indefinitely), no owner check, no S_ISREG check, unbounded read. It
# also swallowed every outcome silently (zero observability) and let
# SystemExit escape the fail-open boundary. These tests pin each defense.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    sys.platform == "win32",
    reason="O_NOFOLLOW is POSIX-only; Windows has no symlink-at-open defense "
    "and relies on the config directory's ACLs.",
)
def test_symlink_extension_refused(m, capsys):
    """C-5: a symlink at the extension path must not be followed."""
    ext_dir = m.CONFIG_DIR / "extensions"
    ext_dir.mkdir(exist_ok=True)
    target = ext_dir / "real.py"
    target.write_text("def run(ctx):\n    return 'symlink ran'\n")
    os.chmod(str(target), 0o600)
    link = ext_dir / "post_flush.py"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "refused" in captured.err.lower() or "eloop" in captured.err.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="O_NONBLOCK + FIFOs are POSIX-only; Windows has no FIFO concept.",
)
def test_fifo_extension_does_not_hang(m, capsys):
    """C-5: a FIFO at the extension path must not block the worker thread."""
    ext_dir = m.CONFIG_DIR / "extensions"
    ext_dir.mkdir(exist_ok=True)
    fifo = ext_dir / "post_flush.py"
    try:
        os.mkfifo(str(fifo), 0o600)
    except OSError:
        pytest.skip("cannot create FIFO on this platform")
    # If the defense is missing, os.open blocks forever and the test times out.
    # O_NONBLOCK makes open return ENXIO immediately.
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "refused" in captured.err.lower() or "no such device" in captured.err.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="owner check is POSIX-only (st_uid); Windows uses ACLs.",
)
def test_owner_mismatch_extension_refused(m, capsys, monkeypatch):
    """C-5: an extension file owned by another user must be refused."""
    _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    # Simulate a different owner by patching os.getuid to return a different uid.
    # The fstat returns the real file's uid (current user), so the check
    # st_uid != os.getuid() fires. Patch on the measure module's os reference
    # so the code sees the patched value.
    real_getuid = os.getuid()
    monkeypatch.setattr(m.os, "getuid", lambda: real_getuid + 9999)
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "owner" in captured.err.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="S_ISREG check is POSIX-only; Windows fstat mode bits differ.",
)
def test_char_device_extension_refused(m, capsys):
    """C-5 / L-3: a char device at the extension path must be refused."""
    ext_dir = m.CONFIG_DIR / "extensions"
    ext_dir.mkdir(exist_ok=True)
    # /dev/null is a universally available char device.
    link = ext_dir / "post_flush.py"
    try:
        os.symlink("/dev/null", link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")
    # O_NOFOLLOW refuses the symlink itself, but if that defense were bypassed,
    # S_ISREG catches the char device. Either way, the extension must not run.
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_system_exit_does_not_crash_worker(m, capsys):
    """H-6: an extension calling sys.exit() must not escape the fail-open
    boundary. SystemExit is a BaseException, not an Exception, so the old
    `except Exception` did not catch it — the worker would terminate."""
    _install_ext(m, "import sys\ndef run(ctx):\n    sys.exit(0)\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "sys.exit" in captured.err.lower()


def test_system_exit_nonzero_does_not_crash_worker(m, capsys):
    """H-6: sys.exit(nonzero) must also be swallowed."""
    _install_ext(m, "import sys\ndef run(ctx):\n    sys.exit(1)\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None


def test_extension_failure_emits_stderr(m, capsys):
    """H-5: a broken extension must emit a [Token Optimizer] stderr line so an
    admin gets feedback instead of silent failure."""
    _install_ext(m, "def run(ctx):\n    raise RuntimeError('exploded')\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "[token optimizer]" in captured.err.lower()
    assert "runtimeerror" in captured.err.lower()


def test_broken_module_emits_stderr(m, capsys):
    """H-5: a syntax error in the extension must emit a stderr line."""
    _install_ext(m, "this is not python (((\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "[token optimizer]" in captured.err.lower()
    assert "failed to load" in captured.err.lower()


def test_missing_run_emits_stderr(m, capsys):
    """H-5: an extension without a callable run() must emit a stderr line."""
    _install_ext(m, "x = 1\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
    captured = capsys.readouterr()
    assert "[token optimizer]" in captured.err.lower()
    assert "no callable run" in captured.err.lower()


def test_successful_extension_emits_stderr(m, capsys):
    """H-5: a successful extension run must emit a stderr confirmation."""
    _install_ext(m, "def run(ctx):\n    return 'ok'\n")
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) == "ok"
    captured = capsys.readouterr()
    assert "[token optimizer]" in captured.err.lower()
    assert "ran successfully" in captured.err.lower()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="file size cap test uses POSIX-only truncate; Windows mode bits differ.",
)
def test_oversized_extension_refused(m, capsys):
    """M-4: an extension file larger than 1 MB must be refused (bounded read
    so a huge or sparse file cannot OOM the worker)."""
    p = _install_ext(m, "def run(ctx):\n    return 'ran'\n")
    # Overwrite with > 1 MB of padding before the run() definition.
    with open(str(p), "w") as f:
        f.write("# padding\n" + "x" * (1_048_576 + 100) + "\n")
    os.chmod(str(p), 0o600)
    # The read is capped at 1 MB, so the run() definition is truncated away.
    # The loader reads the first 1 MB, compiles it (syntax error from the
    # truncated line, or no run() found), and returns None.
    assert m._run_post_flush_extensions(time_left_fn=lambda: 10) is None
