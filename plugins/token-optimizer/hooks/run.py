#!/usr/bin/env python3
"""Cross-platform hook dispatcher.

Invoked from hooks.json via a small bash launcher that locates a usable
Python 3 interpreter on macOS, Linux, and Windows:

  "command": "bash \"${CLAUDE_PLUGIN_ROOT}/hooks/python-launcher.sh\" \"${CLAUDE_PLUGIN_ROOT}/hooks/run.py\" <script-relative-path> [args...]"

The launcher handles Windows-specific gotchas (Program Files spaced paths,
Microsoft Store zero-byte stubs in WindowsApps, py launcher fallback) so
this file can assume it's running under a real Python 3.9+.

This dispatcher resolves the target script under CLAUDE_PLUGIN_ROOT,
checks it exists, and runs it with the same interpreter (sys.executable).
On timeout we kill the child (Popen.kill) to avoid leaking a process
holding the trends.db SQLite lock. Always exits 0 so hook failures never
block the user's tool call.

Windows reap note: module_runner.py runs measure.py IN-PROCESS via
runpy.run_module, so the child proc IS measure.py (the trends.db lock
holder), not a grandchild. On Windows we reap with plain proc.kill()
(TerminateProcess of proc.pid only), NOT taskkill /F /T which would walk
the PPID tree and wrongly kill the detached session-end-flush worker
(the one CREATE_BREAKAWAY_FROM_JOB exists to keep alive). The SIGINT/
SIGTERM handler only fires for console Ctrl+C or in-process os.kill; an
external TerminateProcess from the host bypasses Python handlers entirely.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

# Defense in depth: the launcher script already filters interpreters, but
# if a user's PATH has a stale Python 3.7 that slipped through, bail early
# so later imports don't explode with confusing SyntaxError noise.
if sys.version_info < (3, 9):
    sys.exit(0)


# Module-level handle so the signal handler can reach the active child when
# Claude Code (or any parent) sends SIGTERM/SIGINT to run.py itself. Without
# this, an external kill reaps run.py but orphans the measure.py grandchild,
# which keeps the inherited stdout pipe open and makes the parent hang waiting
# for EOF (the multi-minute stop-hook hang).
_child_proc: subprocess.Popen | None = None


def _reap(proc, posix_sig):
    """Reap the child process. Never raises.

    On Windows, the child proc IS measure.py (module_runner.py runs it
    in-process via runpy.run_module), so a plain ``proc.kill()``
    (TerminateProcess of proc.pid only) releases the trends.db lock without
    walking the PPID tree and killing the detached session-end-flush worker
    (the one CREATE_BREAKAWAY_FROM_JOB exists to keep alive).

    On POSIX, the child is started with ``start_new_session=True`` so it leads
    its own process group; killing the group reaps any grandchildren (the
    launcher chain uses ``exec``, so run.py's PID is the one the host tracks).
    Falls back to ``proc.kill()`` when the group is already gone.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            proc.kill()
        except OSError:
            try:
                sys.stderr.write("run.py: nt reap kill failed\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
    elif hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), posix_sig)
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except OSError:
                try:
                    sys.stderr.write("run.py: posix reap kill failed\n")
                    sys.stderr.flush()
                except (OSError, ValueError):
                    pass
    else:
        try:
            proc.kill()
        except OSError:
            try:
                sys.stderr.write("run.py: fallback reap kill failed\n")
                sys.stderr.flush()
            except (OSError, ValueError):
                pass


def _forward_and_exit(signum, frame):
    """Forward SIGTERM/SIGINT to the child, then exit.

    On Windows this handler only fires for console Ctrl+C or an in-process
    os.kill; an external TerminateProcess from the host bypasses Python
    handlers entirely.
    """
    global _child_proc
    if _child_proc is not None:
        _reap(_child_proc, signal.SIGTERM)
    os._exit(0)


def _check_consent(plugin_root: Path | None = None) -> bool:
    """Return True if consent is given or assumed. Fail-open on any error."""
    try:
        home = Path.home()

        # Resolve config path from env (set by Claude Code before hook invocation)
        plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
        config_path = None
        if plugin_data:
            pd = Path(plugin_data).resolve()
            if not str(pd).startswith(str(home)):
                return True  # Path outside home = skip (fail-open)
            # In-home CLAUDE_PLUGIN_DATA must still be a declared identity of
            # THIS plugin, not a foreign plugin's leaked value sharing the same
            # shared plugins/data root (issue #140 sibling site). Reuse the
            # SAME identity-checked resolver plugin_env.py uses for every other
            # hook script instead of trusting pd raw. If the resolver truly
            # can't be imported (unusual/non-standard plugin layout), fall back
            # to trusting pd -- matching pre-#140 behavior -- so a missing
            # import never blocks the consent check. If it CAN be imported but
            # rejects pd as foreign, fall through to the identical legacy chain
            # a session with no CLAUDE_PLUGIN_DATA at all would use below --
            # never re-trust the raw (possibly leaked) value.
            resolver_ran = False
            identity_checked = None
            if plugin_root is not None:
                try:
                    scripts_dir = plugin_root / "skills" / "token-optimizer" / "scripts"
                    scripts_str = str(scripts_dir)
                    if scripts_str not in sys.path:
                        sys.path.insert(0, scripts_str)
                    from plugin_env import resolve_claude_plugin_data_env
                    resolver_ran = True
                    identity_checked = resolve_claude_plugin_data_env()
                except Exception:
                    resolver_ran = False
            if identity_checked is not None:
                config_path = identity_checked / "config" / "config.json"
            elif not resolver_ran:
                config_path = pd / "config" / "config.json"
            # else: resolver ran and rejected pd as a foreign identity --
            # config_path stays None and falls through to the legacy chain.

        if config_path is None:
            # Legacy / Codex fallback
            codex_home = os.environ.get("CODEX_HOME", "")
            if codex_home:
                ch = Path(codex_home).resolve()
                if not str(ch).startswith(str(home)):
                    return True
                config_path = ch / "token-optimizer" / "config.json"
            else:
                # Honor CLAUDE_CONFIG_DIR (Claude Code's official config-dir
                # override) before falling back to ~/.claude. Mirrors
                # runtime_env.claude_home(): accept any absolute, existing,
                # non-symlink directory (CLAUDE_CONFIG_DIR may legitimately live
                # OUTSIDE $HOME — containers, CI), reject relative/symlink, else
                # fall back. The previous str.startswith($HOME) check both
                # excluded valid out-of-home dirs and sibling-prefix-matched
                # (/Users/alex-evil passing for /Users/alex).
                claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
                cc = None
                if claude_config:
                    candidate = Path(claude_config).expanduser()
                    try:
                        if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
                            cc = candidate.resolve()
                    except OSError:
                        cc = None
                if cc is not None:
                    config_path = cc / "token-optimizer" / "config.json"
                else:
                    config_path = home / ".claude" / "token-optimizer" / "config.json"

        if not config_path.exists() or config_path.is_symlink():
            return True  # No config or symlink = fail-open

        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)

        if config.get("enterprise_consent_shown"):
            return True

        # Backward compat backfill: existing users who saw v5 welcome have
        # implicitly consented -- but ONLY when enterprise_consent_shown was
        # never written. A present-and-False enterprise_consent_shown is an
        # explicit opt-out (`consent --reset`); backfilling over it would
        # silently re-enable a user who opted out.
        if config.get("v5_welcome_shown") and "enterprise_consent_shown" not in config:
            config["enterprise_consent_shown"] = True
            # Atomic write (tempfile + os.replace)
            import tempfile
            fd, tmp = tempfile.mkstemp(dir=str(config_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    json.dump(config, tf, indent=2)
                os.replace(tmp, str(config_path))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return True

        # Flags ABSENT is the SessionStart race window (v5.11.93 silent
        # no-op), NOT an opt-out: config.json can be created by a non-consent
        # writer (ensure-health's last_hook_heal_check, a v5 feature toggle)
        # before the consent bootstrap writes the flags. Fail OPEN so
        # non-exempt hooks -- the PreToolUse Bash compression rewrite
        # included -- keep working in that window; the exempt bootstrap
        # (ensure-health/consent/v5) writes the flags shortly after. A
        # consent key PRESENT and False is a genuine explicit opt-out
        # (`measure.py consent --reset`) and stays gated.
        if "enterprise_consent_shown" not in config and "v5_welcome_shown" not in config:
            return True

        return False  # Explicit opt-out (a consent key was written False)
    except Exception:
        return True  # Fail-open: never block on errors


def _windows_stdio_kwargs():
    """Return usable inherited standard handles for a no-window child."""
    kwargs = {}
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        kwargs[name] = stream
    return kwargs


def _claude_settings_path() -> Path | None:
    """Resolve the host's settings.json path, honoring CLAUDE_CONFIG_DIR.

    Mirrors the CLAUDE_CONFIG_DIR handling in the enterprise-consent resolver
    above and in runtime_env.claude_home(): accept any absolute, existing,
    non-symlink directory (CLAUDE_CONFIG_DIR may legitimately live OUTSIDE
    $HOME -- containers, CI runners, relocated config volumes), reject
    relative/symlink, else fall back to ~/.claude. Without this the disable
    self-check read the wrong file for every CLAUDE_CONFIG_DIR user and the
    feature silently no-opped for exactly the population the repo otherwise
    supports (test_claude_config_dir, test_host_safety_guard, etc.).
    Returns None when no usable settings.json exists.
    """
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base = None
    if claude_config:
        candidate = Path(claude_config).expanduser()
        try:
            if candidate.is_absolute() and candidate.is_dir() and not candidate.is_symlink():
                base = candidate.resolve()
        except OSError:
            base = None
    if base is None:
        base = Path.home() / ".claude"
    settings_path = base / "settings.json"
    if not settings_path.is_file() or settings_path.is_symlink():
        return None
    return settings_path


def _plugin_disabled_by_host() -> bool:
    """Return True if the host explicitly turned this plugin off via the host
    settings.json's enabledPlugins map, so main() can no-op before spawning the
    dispatch subprocess at all.

    Claude Code's plugin loader does not appear to reliably stop invoking an
    already-registered plugin's hooks.json commands for existing sessions
    after enabledPlugins[<name>@<marketplace>] is flipped to false (observed:
    8/8 sessions started after such an edit still ran hooks and printed
    "[Token Optimizer]" output). This is a defensive self-check so the plugin
    honors its own disable flag even when the host does not enforce it,
    instead of silently continuing to spend tokens on every hook event.

    The settings path is resolved via _claude_settings_path(), which honors
    CLAUDE_CONFIG_DIR (the consent resolver and runtime_env.claude_home() both
    do); hardcoding ~/.claude/settings.json left every CLAUDE_CONFIG_DIR user
    reading the wrong file and never getting the disable honored.

    Fail-open (return False) on any error -- a missing/unreadable settings
    file, or a plugin/marketplace name we can't resolve, must never silently
    disable the plugin for users who never touched this setting.
    """
    try:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
        if not plugin_root:
            return False
        meta_dir = Path(plugin_root) / ".claude-plugin"
        plugin_json = meta_dir / "plugin.json"
        marketplace_json = meta_dir / "marketplace.json"
        if not plugin_json.is_file() or not marketplace_json.is_file():
            return False
        # Cap reads: this runs on every hook event (a fresh process each time, so
        # nothing can be cached across events). settings.json is user-controlled and
        # could be pathologically large; an oversized config reads as "can't tell" ->
        # fail-open (plugin treated as enabled), the safe default.
        _CFG_MAX = 4_000_000
        for _cfg in (plugin_json, marketplace_json):
            if _cfg.stat().st_size > _CFG_MAX:
                return False
        plugin_name = json.loads(plugin_json.read_text(encoding="utf-8")).get("name", "").strip()
        marketplace_name = json.loads(marketplace_json.read_text(encoding="utf-8")).get("name", "").strip()
        if not plugin_name or not marketplace_name:
            return False
        settings_path = _claude_settings_path()
        if settings_path is None:
            return False
        if settings_path.stat().st_size > _CFG_MAX:
            return False
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        enabled_plugins = settings.get("enabledPlugins")
        if not isinstance(enabled_plugins, dict):
            return False
        key = f"{plugin_name}@{marketplace_name}"
        return enabled_plugins.get(key) is False
    except Exception:
        return False


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    if _plugin_disabled_by_host():
        return 0

    script_rel = sys.argv[1]
    script_args = sys.argv[2:]

    # Issue #139: whole-UserPromptSubmit-path opt-out. Checked BEFORE the
    # module_runner command is built (no stdin read, no imports, no fs access)
    # so a user who sets TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT=0 pays zero
    # per-prompt cost. The consolidated dispatcher lives at this relative path;
    # the gate is exact-target so it never silences any other hook script.
    if script_rel == "hooks/userpromptsubmit_runner.py":
        if os.environ.get("TOKEN_OPTIMIZER_HOOKS_USERPROMPTSUBMIT", "").strip() == "0":
            return 0

    rel_path = Path(script_rel)
    if rel_path.is_absolute() or ".." in rel_path.parts:
        return 0

    plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
    if plugin_root:
        root_path = Path(plugin_root)
    else:
        # Fallback: relative to this wrapper's parent directory.
        root_path = Path(__file__).resolve().parent.parent

    try:
        root_resolved = root_path.resolve(strict=True)
        candidate = root_resolved / rel_path
        if not candidate.is_file():
            return 0
        script_path = candidate.resolve(strict=True)
        if not script_path.is_relative_to(root_resolved):
            return 0
    except (OSError, ValueError):
        return 0

    # Use the interpreter that ran this wrapper so we inherit the correct
    # Python across macOS/Linux/Windows without relying on PATH.
    #
    # Dispatch through module_runner.py rather than running script_path
    # directly: CPython never caches __pycache__ bytecode for a script run as
    # __main__, only for imported modules, so a direct `python script_path`
    # recompiles the target from source on every single hook invocation. For
    # measure.py (35k+ lines) that is ~0.3s of pure parse/compile paid on
    # nearly every tool call. module_runner.py runs it as a module instead, so
    # the import system's normal bytecode cache applies. See module_runner.py.
    module_runner = Path(__file__).resolve().parent / "module_runner.py"
    cmd = [sys.executable, str(module_runner), str(script_path.parent), script_path.stem, *script_args]

    # Consent gate: skip data collection until acknowledged.
    # EXEMPT: ensure-health and consent commands bootstrap the consent flag itself.
    # Blocking them creates a deadlock (config.json exists without flags -> ensure-health
    # can't run -> flags never written -> plugin permanently inert).
    exempt_commands = {"ensure-health", "consent", "v5"}
    is_exempt = any(arg in exempt_commands for arg in script_args[:2])
    # Issue #139 P0: the consolidated UserPromptSubmit runner is dispatched as
    # `run.py hooks/userpromptsubmit_runner.py` with NO trailing args, so
    # script_args=[] and the exempt_commands check above never matches it. The
    # runner contains the ensure-health bootstrap itself, so it MUST be let
    # through this consent gate even when consent is False; the runner then
    # makes the per-subcommand consent decision internally (ensure-health
    # bootstraps the flags, the other five skip until consent is True). Without
    # this exemption the runner is never dispatched when consent is False ->
    # ensure-health never fires -> v5_welcome_shown / enterprise_consent_shown
    # never written -> consent False FOREVER -> all six subcommands permanently
    # dead. Latent on native Claude Code (SessionStart bootstraps consent
    # out-of-band), FATAL on Cowork (no SessionStart hook).
    if script_rel == "hooks/userpromptsubmit_runner.py":
        is_exempt = True
    if not is_exempt and not _check_consent(root_resolved):
        return 0

    # Force UTF-8 in every dispatched script regardless of the host locale, so
    # non-ASCII session paths / transcript content (Hebrew, CJK, accented names)
    # never crash a hook with UnicodeDecode/EncodeError. PYTHONUTF8 also makes the
    # child's default open() encoding UTF-8; PYTHONIOENCODING covers its std streams.
    child_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}

    proc = None
    global _child_proc
    # Install SIGTERM/SIGINT handlers BEFORE spawning the child so an external
    # kill from Claude Code reaps the whole child process group instead of
    # orphaning the grandchild. On Windows these handlers only fire for
    # console Ctrl+C or in-process os.kill; an external TerminateProcess from
    # the host bypasses Python handlers entirely.
    signal.signal(signal.SIGTERM, _forward_and_exit)
    signal.signal(signal.SIGINT, _forward_and_exit)
    try:
        # start_new_session=True puts the child in its own process group so a
        # timeout/external kill can reap the whole group (grandchildren included)
        # via os.killpg. Do NOT add stdout=/stderr=/stdin= here: several hooks
        # inject via stdout and MUST inherit run.py's stdio.
        # On Windows, start_new_session is a no-op; use CREATE_NO_WINDOW to hide
        # the console flash. Do NOT add CREATE_NEW_PROCESS_GROUP (inert for
        # reaping here since run.py never sends GenerateConsoleCtrlEvent, and
        # it disables the child's Ctrl+C self-terminate). Do NOT use
        # DETACHED_PROCESS -- the child MUST inherit run.py's stdio for hook
        # injection via stdout.
        _popen_kwargs = dict(env=child_env)
        if os.name == "nt":
            _flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if _flags:
                _popen_kwargs["creationflags"] = _flags
            _popen_kwargs.update(_windows_stdio_kwargs())
        else:
            _popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **_popen_kwargs)
        _child_proc = proc
        try:
            proc.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Important: Popen.wait doesn't auto-kill on timeout. Leaving
            # the child alive would leak a process holding the trends.db
            # SQLite lock, starving the next hook invocation. _reap handles
            # the nt/posix split (see _reap docstring).
            try:
                # signal.SIGKILL does not exist on Windows (AttributeError
                # at the call site, before _reap even runs). Use getattr so
                # the nt branch of _reap (which ignores posix_sig) is never
                # blocked by a missing attribute. POSIX has SIGKILL.
                _reap(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
                proc.wait(timeout=5)
            except (subprocess.SubprocessError, OSError):
                pass
    except (subprocess.SubprocessError, OSError):
        if proc is not None:
            try:
                proc.kill()
            except (subprocess.SubprocessError, OSError):
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
