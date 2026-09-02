#!/usr/bin/env python3
"""Token Optimizer — Cursor hook bridge.

Thin, fast, fail-safe entry point invoked by Cursor's hooks.json (merged into
``~/.cursor/hooks.json``). One bridge handles every Cursor event TO wires:

    cursor_hook_bridge.py sessionStart
    cursor_hook_bridge.py preToolUse
    cursor_hook_bridge.py postToolUse
    cursor_hook_bridge.py preCompact
    cursor_hook_bridge.py stop
    cursor_hook_bridge.py sessionEnd

Contract notes (verified against cursor.com/docs/hooks and the installed
Cursor CLI bundle as of 2026-08-31, see .sprint-scratch/cursor-research.md):

- Payloads arrive on stdin as ONE snake_case JSON object. Common fields:
  ``conversation_id``, ``generation_id``, ``model``, ``model_id``,
  ``hook_event_name``, ``cursor_version``, ``workspace_roots``,
  ``transcript_path``. ``tool_input`` may be an object or a JSON-encoded
  string (either tolerated); anything undecodable is a silent no-op.
- stdin → stdout JSON, exit 0 to accept. Exit 2 would deny (never emitted:
  TO never blocks). Non-0 non-2 fails open in Cursor, but we always exit 0.
- No capability matrix. Cursor hooks are observed, not version-gated: every
  handler appends one line to ``observed-events.jsonl`` so ``cursor-doctor``
  can say exactly which events have fired on this install (KTD7/R13).
- Output is Cursor's top-level contract (NOT the Copilot
  ``hookSpecificOutput`` envelope): ``preToolUse`` → ``{"permission":
  "allow", "updated_input": {...}}``; ``sessionStart`` / ``postToolUse`` →
  ``{"additional_context": "..."}``; the rest emit nothing.

Security posture mirrors bash_hook.py: the ``Shell`` rewrite inherits its
whitelist + dangerous-char exclusions and fails CLOSED (emit nothing) on any
uncertainty. This bridge never imports Cursor internals and never writes
outside ``<cursor_home>/token-optimizer/``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows console-flash guard (#107). Cursor's hook runners launch hooks
# directly, so any child we spawn would otherwise allocate a console window on
# Windows. getattr -> 0 on POSIX (creationflags=0 is an accepted no-op).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

# The bridge must run even if siblings are missing (partial install): each
# import below is optional, with the dependent feature degraded/disabled.
try:
    from runtime_env import cursor_home
except ImportError:  # pragma: no cover - broken install
    cursor_home = None  # type: ignore[assignment]

try:
    from spawn_utils import spawn_detached
except ImportError:  # pragma: no cover - broken install
    logger.warning("[cursor] spawn_utils import failed; using degraded fallback")

    def spawn_detached(argv, **popen_kwargs):  # type: ignore[no-redef]
        # Degraded broken-install path: does NOT detach (see
        # spawn_utils.spawn_detached for the real OS-flag logic + breakaway
        # retry). It DOES OR-in CREATE_NO_WINDOW (#107) so a broken install
        # degrades to "child dies with the parent" rather than "console window
        # flashes on every stop/sessionEnd hook".
        import subprocess as _sp

        kwargs = dict(popen_kwargs)
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | _NO_WINDOW
        try:
            return _sp.Popen(argv, **kwargs)
        except OSError:
            return None

try:
    import bash_hook as _bash_hook
except ImportError:  # pragma: no cover
    _bash_hook = None  # type: ignore[assignment]

try:
    from codex_io import atomic_write_json as _atomic_write_json_impl
except ImportError:  # pragma: no cover - broken install
    _atomic_write_json_impl = None  # type: ignore[assignment]

try:
    from hook_runtime import lease_lock
except ImportError:  # pragma: no cover - broken install
    lease_lock = None  # type: ignore[assignment]

_MAX_STDIN_BYTES = 4 * 1024 * 1024  # refuse absurd payloads (amplification)
_RESTORE_MAX_BYTES = 16_384        # restore-context files above this are ignored
_ACTIVE_WORKSPACE_SECS = 600        # skip restore when a sibling tally is live
_STOP_ROLLUP_SECS = 120             # R12: at most one stop rollup per machine per 120s
_STALE_LOCK_SECS = 7 * 24 * 3600    # sessionStart lock sweep threshold
_NUDGE_TOOL_CALLS = (30, 80)        # context-growth nudge thresholds (turn proxy)

# Snapshot at import: the installed payload is static for the process lifetime.
_COMPRESS_PATH = _SCRIPT_DIR / "bash_compress.py"
_COMPRESS_AVAILABLE = _COMPRESS_PATH.exists()

_SESSION_ID_RE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_session_id(sid):
    """Strip everything but [A-Za-z0-9_-] so a payload id can never traverse.

    Mirrors measure.sanitize_session_id / copilot_hook_bridge: a conversation_id
    like "../../../etc/passwd" would otherwise let _atomic_write_json / the lock
    path escape the data dir.
    """
    if not sid:
        return "unknown"
    cleaned = _SESSION_ID_RE.sub("", sid)[:64]
    return cleaned if len(cleaned) >= 6 else "unknown"


def _read_stdin_payload():
    """Read and decode the Cursor hook payload from stdin. None on any failure."""
    try:
        raw = sys.stdin.read(_MAX_STDIN_BYTES + 1)
    except (OSError, UnicodeDecodeError):
        return None
    if not raw or len(raw) > _MAX_STDIN_BYTES:
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_tool_input(raw):
    """tool_input as a dict, or as a JSON-encoded string -> dict; {} otherwise."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return {}


def decode_payload(payload):
    """Normalize a Cursor hook payload into a flat, safe dict.

    ``conversation_id`` wins over ``session_id`` (sessionStart carries only
    session_id; the tool events carry conversation_id). ``workspace_roots`` is
    the list Cursor exports; ``cwd`` is capped at 1024 chars because it is
    rewritten into the tally on every tool call and into restore-context.
    Missing fields default to empty values so callers never KeyError.
    """
    out = {
        "tool_name": "",
        "tool_args": {},
        "conversation_id": "",
        "hook_event_name": "",
        "cwd": "",
        "cursor_version": "",
        "workspace_roots": [],
        "transcript_path": None,
        "model": None,
        "timestamp": None,
    }
    if not isinstance(payload, dict):
        return out

    tool_name = payload.get("tool_name", payload.get("toolName", ""))
    if isinstance(tool_name, str):
        out["tool_name"] = tool_name.strip()
    out["tool_args"] = _parse_tool_input(
        payload.get("tool_input", payload.get("toolInput", {}))
    )

    sid = payload.get("conversation_id", payload.get("session_id", ""))
    if isinstance(sid, str):
        out["conversation_id"] = _sanitize_session_id(sid)

    event = payload.get("hook_event_name", "")
    if isinstance(event, str):
        out["hook_event_name"] = event.strip()

    cwd = payload.get("cwd", "")
    if isinstance(cwd, str):
        out["cwd"] = cwd[:1024]

    version = payload.get("cursor_version", "")
    if isinstance(version, str):
        out["cursor_version"] = version[:64]

    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        out["workspace_roots"] = [
            str(r)[:1024] for r in roots if isinstance(r, str)
        ][:8]

    tp = payload.get("transcript_path")
    if isinstance(tp, str):
        out["transcript_path"] = tp[:1024]

    model = payload.get("model")
    if isinstance(model, str):
        out["model"] = model[:128]

    out["timestamp"] = payload.get("timestamp")
    return out


# ---------------------------------------------------------------------------
# Data dir + atomic write
# ---------------------------------------------------------------------------


def _to_dir():
    """Token Optimizer's data dir under the Cursor home. None if unavailable."""
    if cursor_home is None:
        return None
    try:
        d = cursor_home() / "token-optimizer"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _sessions_dir():
    """<cursor_home>/token-optimizer/sessions/ — the durable tally store."""
    to_dir = _to_dir()
    if to_dir is None:
        return None
    try:
        d = to_dir / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d
    except OSError:
        return None


def _atomic_write_json(path, obj):
    """Atomic JSON write via codex_io; concurrent writers never corrupt."""
    if _atomic_write_json_impl is None:
        return False
    try:
        _atomic_write_json_impl(path, obj)
        return True
    except OSError:
        return False


def _record_observed(event, **extra):
    """Append one observed-events line (KTD7/R13). Single-line JSONL append.

    Each event writes one line; the doctor tolerates a torn final line and sums
    the rest. cursor_version comes from the hook env first (Cursor exports it),
    then the payload. Never raises — the ledger is observational, not gating.
    """
    to_dir = _to_dir()
    if to_dir is None:
        return
    entry = {
        "event": event,
        "cursor_version": str(os.environ.get("CURSOR_VERSION") or "").strip()[:64],
        "ts": time.time(),
    }
    entry.update({k: v for k, v in extra.items() if v is not None})
    try:
        with (to_dir / "observed-events.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        logger.debug("[cursor_hook_bridge] observed-events append failed", exc_info=True)


# ---------------------------------------------------------------------------
# Tally read-modify-write (durable session record)
# ---------------------------------------------------------------------------


def _tally_path(fields):
    sdir = _sessions_dir()
    if sdir is None:
        return None
    sid = fields.get("conversation_id") or "unknown"
    return sdir / f"{sid}.json"


def _load_tally(path):
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError, ValueError):
        return None


@contextmanager
def _session_lock(to_dir, sid):
    """Bounded portable lease around a tally read-modify-write.

    Fail-open on contention: a contender skips after 75ms rather than blocking
    a Cursor hot path. The tally is a soft counter, so a skipped update is
    tolerable. A missing ``hook_runtime`` (broken install) proceeds unlocked.
    """
    if to_dir is None or sid in (None, "", "unknown"):
        yield False
        return
    lock_dir = to_dir / ".locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        yield True  # fail open: tally update proceeds, just unlocked
        return
    if lease_lock is None:
        yield True
        return
    lock_path = lock_dir / f"{sid}.lock"
    with lease_lock(lock_path, acquire_timeout=0.075) as acquired:
        yield acquired


def _update_tally(fields, *, terminal=False, end_reason=None,
                  increment_turns=False, count_tool=False, compaction=None):
    """Read-modify-write the session tally; returns the tally or None.

    Non-terminal activity reopens an idle/sessionEnd-finalised tally (final set
    back to False) so a long-lived IDE composer resumed after lunch is never
    frozen at the old ``idle`` verdict. ``terminal=True`` is sessionEnd only.
    """
    to_dir = _to_dir()
    path = _tally_path(fields) if to_dir is not None else None
    if path is None:
        return None
    sid = fields.get("conversation_id") or "unknown"
    with _session_lock(to_dir, sid) as acquired:
        if not acquired:
            return None
        tally = _load_tally(path)
        now = time.time()
        if tally is None:
            tally = {"conversation_id": sid, "first_ts": now}

        if terminal:
            tally["final"] = True
            tally["end_reason"] = end_reason or "sessionEnd"
        else:
            if tally.get("final") or tally.get("end_reason") in ("idle", "sessionEnd"):
                tally["final"] = False
                tally["end_reason"] = ""

        tally["updated_at"] = now
        tally.setdefault("turns", 0)
        tally.setdefault("tool_calls", 0)
        tally.setdefault("models", {})
        tally.setdefault("tool_names", {})
        tally.setdefault("compactions", [])

        if fields.get("cwd"):
            tally["cwd"] = fields["cwd"]
        if fields.get("workspace_roots"):
            tally["workspace_roots"] = fields["workspace_roots"]
        if fields.get("transcript_path") is not None:
            tally["transcript_path"] = fields["transcript_path"]
        if fields.get("cursor_version"):
            tally["cursor_version"] = fields["cursor_version"]

        model = fields.get("model")
        if isinstance(model, str) and model:
            models = tally.setdefault("models", {})
            models[model] = int(models.get(model, 0) or 0) + 1

        if count_tool:
            tally["tool_calls"] = int(tally.get("tool_calls", 0) or 0) + 1
            name = fields.get("tool_name") or "tool"
            names = tally.setdefault("tool_names", {})
            names[name] = int(names.get(name, 0) or 0) + 1

        if increment_turns:
            tally["turns"] = int(tally.get("turns", 0) or 0) + 1

        if compaction is not None:
            comps = tally.setdefault("compactions", [])
            comps.append(compaction)
            tally["compactions"] = comps[-200:]

        _atomic_write_json(path, tally)
        return tally


# ---------------------------------------------------------------------------
# Restore context (per workspace — R8, review finding)
# ---------------------------------------------------------------------------


def _workspace_root(fields):
    roots = fields.get("workspace_roots") or []
    if roots and isinstance(roots[0], str) and roots[0]:
        return roots[0]
    return fields.get("cwd") or None


def _restore_for_workspace(fields):
    """Return the restore-context for THIS workspace, or None.

    Cursor IDE runs many concurrent chats across repos, so a single global
    restore file would seed every new chat with whichever conversation rolled
    up last. Files are keyed by ``sha1(workspace_root)`` and injected only for
    the matching root. Injection is skipped while a non-final sibling tally in
    that workspace has activity in the last 10 minutes.
    """
    root = _workspace_root(fields)
    to_dir = _to_dir()
    if to_dir is None or not root:
        return None

    sdir = _sessions_dir()
    now = time.time()
    own_id = fields.get("conversation_id") or ""
    if sdir is not None:
        try:
            for p in sdir.glob("*.json"):
                t = _load_tally(p)
                if not t or t.get("final"):
                    continue
                # Exclude the session being started: its own freshly-written
                # tally is active by definition and must not suppress restore.
                if own_id and str(t.get("conversation_id") or "") == own_id:
                    continue
                troot = None
                wroots = t.get("workspace_roots")
                if isinstance(wroots, list) and wroots and isinstance(wroots[0], str):
                    troot = wroots[0]
                else:
                    troot = t.get("cwd")
                if str(troot) == str(root):
                    try:
                        if now - float(t.get("updated_at") or 0) < _ACTIVE_WORKSPACE_SECS:
                            return None
                    except (TypeError, ValueError):
                        pass
        except OSError:
            pass

    digest = hashlib.sha1(root.encode("utf-8", "replace")).hexdigest()
    path = to_dir / "restore-context" / f"{digest}.md"
    try:
        if path.exists() and path.stat().st_size <= _RESTORE_MAX_BYTES:
            text = path.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# Compression + nudge helpers
# ---------------------------------------------------------------------------


def _compression_enabled():
    return (
        _bash_hook is not None
        and _COMPRESS_AVAILABLE
        and os.environ.get("TOKEN_OPTIMIZER_BASH_COMPRESS", "").strip() != "0"
    )


def _nudge_level(tool_calls):
    level = 0
    for threshold in _NUDGE_TOOL_CALLS:
        if tool_calls >= threshold:
            level += 1
    return level


_NUDGE_TEXT = (
    "[Token Optimizer] Context is growing large for this session. Prefer "
    "targeted reads over full files, avoid re-reading unchanged files, and "
    "summarize before continuing long explorations."
)


# ---------------------------------------------------------------------------
# Spawns (fire-and-forget; detach + CREATE_NO_WINDOW)
# ---------------------------------------------------------------------------


def _measure_env(interactive=False):
    env = dict(os.environ)
    env["TOKEN_OPTIMIZER_RUNTIME"] = "cursor"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if interactive:
        env["TOKEN_OPTIMIZER_INTERACTIVE"] = "1"
    return env


def _spawn_rollup():
    measure = _SCRIPT_DIR / "measure.py"
    if not measure.exists():
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "cursor-rollup", "--quiet"],
            env=_measure_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _spawn_dashboard():
    measure = _SCRIPT_DIR / "measure.py"
    if not measure.exists():
        return
    try:
        spawn_detached(
            [sys.executable, str(measure), "dashboard", "--quiet"],
            env=_measure_env(interactive=True),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _stop_rollup_due():
    """True once per 120s per machine (R12). Best-effort: reads then writes."""
    to_dir = _to_dir()
    if to_dir is None:
        return False
    state = to_dir / ".stop-rollup-last.json"
    now = time.time()
    last = None
    try:
        if state.exists():
            data = json.loads(state.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                last = float(data.get("last", 0) or 0)
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        last = None
    if last is not None and now - last < _STOP_ROLLUP_SECS:
        return False
    _atomic_write_json(state, {"last": now})
    return True


def _sweep_stale_locks():
    """Remove stale lock/candidate artifacts (7d) on sessionStart. Never fatal."""
    to_dir = _to_dir()
    if to_dir is None:
        return
    lock_dir = to_dir / ".locks"
    if not lock_dir.is_dir():
        return
    now = time.time()
    try:
        for pattern in ("*.lock", ".*.lock.candidate-*"):
            for p in lock_dir.glob(pattern):
                try:
                    if now - p.stat().st_mtime > _STALE_LOCK_SECS:
                        p.unlink()
                except OSError:
                    continue
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Event handlers (all fail-open: never raise into Cursor)
# ---------------------------------------------------------------------------


def _emit(obj):
    print(json.dumps(obj))


def handle_session_start(payload):
    """Sweep stale locks, re/init the tally, inject per-workspace continuity."""
    fields = decode_payload(payload)
    _record_observed("sessionStart",
                     conversation_id=fields["conversation_id"] or None)
    _update_tally(fields)
    _sweep_stale_locks()

    restore = _restore_for_workspace(fields)
    if restore:
        _emit({"additional_context": restore})


def handle_pre_tool_use(payload):
    """Shell bash-compression via updated_input, gated and fail-closed."""
    fields = decode_payload(payload)
    rewrite = None
    if fields["tool_name"] == "Shell" and _compression_enabled():
        command = fields["tool_args"].get("command")
        if isinstance(command, str) and command:
            if not _bash_hook._has_dangerous_chars(command) and _bash_hook._is_whitelisted(command):
                try:
                    original_tokens = shlex.split(command)
                except ValueError:
                    original_tokens = None
                if original_tokens:
                    rewritten = (
                        shlex.quote(sys.executable)
                        + " " + shlex.quote(str(_COMPRESS_PATH))
                        + " " + " ".join(shlex.quote(t) for t in original_tokens)
                    )
                    # Echo every original field and replace only `command` so
                    # working_directory and any future fields survive the
                    # whole-tool_input replacement contract.
                    updated = dict(fields["tool_args"])
                    updated["command"] = rewritten
                    _emit({"permission": "allow", "updated_input": updated})
                    rewrite = "attempted"
    _record_observed("preToolUse",
                     tool_name=fields["tool_name"] or None,
                     rewrite=rewrite)


def handle_post_tool_use(payload):
    """Update the tally and, for Shell, record whether the rewrite was honoured.

    Cursor's postToolUse tool_input carries the command as actually executed:
    ``command`` containing ``bash_compress.py`` proves the rewrite was honoured;
    a whitelisted command that arrived bare proves it was ignored.
    """
    fields = decode_payload(payload)
    rewrite = None
    if fields["tool_name"] == "Shell" and _compression_enabled():
        command = fields["tool_args"].get("command")
        if isinstance(command, str) and command:
            if not _bash_hook._has_dangerous_chars(command) and _bash_hook._is_whitelisted(command):
                rewrite = "honoured" if "bash_compress.py" in command else "ignored"
    _record_observed("postToolUse",
                     tool_name=fields["tool_name"] or None,
                     rewrite=rewrite)

    tally = _update_tally(fields, count_tool=True)
    if tally is None:
        return

    # Context-growth nudge (R10). preCompact gives real numbers; when it never
    # fired the tool-call count is the honest available proxy.
    tool_calls = int(tally.get("tool_calls", 0) or 0)
    new_level = _nudge_level(tool_calls)
    old_level = int(tally.get("nudge_level", 0) or 0)
    if new_level > old_level:
        with _session_lock(_to_dir(), fields["conversation_id"]) as acquired:
            if not acquired:
                return
            # Re-read under the lock so the nudge_level write is not lost to a
            # concurrent sibling chat writing the same tally.
            tpath = _tally_path(fields)
            t = _load_tally(tpath) or tally
            t["nudge_level"] = new_level
            _atomic_write_json(tpath, t)
        _emit({"additional_context": _NUDGE_TEXT})


def handle_pre_compact(payload):
    """Record Cursor's real context numbers and compaction trigger."""
    fields = decode_payload(payload)
    _record_observed("preCompact", conversation_id=fields["conversation_id"] or None)
    try:
        ctx_tokens = int(payload.get("context_tokens") or 0)
    except (TypeError, ValueError):
        ctx_tokens = 0
    try:
        ctx_window = int(payload.get("context_window_size") or 0)
    except (TypeError, ValueError):
        ctx_window = 0
    try:
        usage_pct = float(payload.get("context_usage_percent") or 0.0)
    except (TypeError, ValueError):
        usage_pct = 0.0
    _update_tally(
        fields,
        compaction={
            "trigger": str(payload.get("trigger") or "")[:64],
            "context_tokens": ctx_tokens,
            "context_window_size": ctx_window,
            "context_usage_percent": usage_pct,
            "ts": time.time(),
        },
    )


def handle_stop(payload):
    """Per-turn tally + throttled detached rollup and dashboard refresh."""
    fields = decode_payload(payload)
    _record_observed("stop", conversation_id=fields["conversation_id"] or None)
    _update_tally(fields, increment_turns=True)
    if _stop_rollup_due():
        _spawn_rollup()
        _spawn_dashboard()


def handle_session_end(payload):
    """Mark the tally final and spawn an unthrottled rollup + dashboard."""
    fields = decode_payload(payload)
    reason = payload.get("reason") if isinstance(payload, dict) else None
    _record_observed("sessionEnd", conversation_id=fields["conversation_id"] or None)
    _update_tally(fields, terminal=True,
                  end_reason=str(reason)[:64] if reason else "sessionEnd")
    _spawn_rollup()
    _spawn_dashboard()


_HANDLERS = {
    "sessionStart": handle_session_start,
    "preToolUse": handle_pre_tool_use,
    "postToolUse": handle_post_tool_use,
    "preCompact": handle_pre_compact,
    "stop": handle_stop,
    "sessionEnd": handle_session_end,
}


def main(argv=None):
    try:
        from utf8_io import enforce_utf8_io
        enforce_utf8_io()
    except Exception:
        pass
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in _HANDLERS:
        return 0
    os.environ.setdefault("TOKEN_OPTIMIZER_RUNTIME", "cursor")
    payload = _read_stdin_payload()
    if payload is None:
        payload = {}
    try:
        _HANDLERS[args[0]](payload)
    except Exception:
        # A hook must never break the user's Cursor session. No output, exit 0.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
