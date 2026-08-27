#!/usr/bin/env python3
"""Token Optimizer v5.12: Pipeline-aware read-only safety classifier.

Used by the PostToolUse bash_compress_hook.py to decide whether a pipeline
or metachar-containing command is safe to compress. Unlike the PreToolUse
bash_hook.py (which categorically rejects metachar commands), this module
SPLITS the command into pipeline stages and checks EACH stage individually
against a consolidated read-only whitelist.

A pipeline is eligible for compression ONLY when EVERY stage is a known
read-only command. Any unrecognized, side-effecting, or unparseable stage
causes the whole pipeline to be rejected (fail-open: pass through raw).

The consolidated whitelist merges bash_hook._WHITELIST_SINGLE,
bash_hook._WHITELIST_COMPOUND, and additional pipeline-consumer commands
(head/tail/wc/sort/uniq/cut/tr/awk/sed/tee/column/etc.).

Security invariants:
- Read-only only: no write, no side-effect, no interpreter
- Fail-open default: unknown command → not read-only → pass through raw
- No shell=True, no command reconstruction, no re-execution
"""

from __future__ import annotations

import shlex

# ---------------------------------------------------------------------------
# Pipeline separators (tokens that split one command into multiple stages).
# These must match shlex.split() output exactly; they are single tokens.
# ---------------------------------------------------------------------------
_PIPELINE_SEPARATORS = frozenset({"|", "|&", "&&", "||", ";"})

# ---------------------------------------------------------------------------
# Redirection tokens to strip from a stage before whitelist check.
# A token is a redirection if:
#   - It IS one of: ">", ">>", "<", "<<", "<>", ">&", "&>"
#   - It STARTS WITH a digit and contains ">" or "&" (like "2>&1", "1>&2")
# ---------------------------------------------------------------------------
_REDIRECT_TOKENS = frozenset({">", ">>", "<", "<<", "<>", ">&", "&>"})


def _is_redirect_token(tok: str) -> bool:
    """True if token is a shell redirection operator."""
    if tok in _REDIRECT_TOKENS:
        return True
    # Numeric redirects: 2>&1, 1>&2, 2>/dev/null, etc.
    # Pattern: optional digit(s) followed by > or >> or < or &>
    if any(c.isdigit() for c in tok[:1]) and any(c in tok for c in (">", "<")):
        # Strip leading digits and check remaining is a redirect
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        rest = tok[i:]
        # "2>&1" → rest = ">&1" → starts with redirect
        if rest and rest[0] in (">", "<", "&"):
            return True
        # "2>/dev/null" → rest = ">/dev/null" → starts with >
        if rest.startswith(">") or rest.startswith("<"):
            return True
    return False


def _redirect_has_file_target(tok: str) -> bool:
    """True if this redirect token expects a filename target.

    Redirects like '>', '>>', '<', '<<', '2>', '1>' consume the next token
    as a filename. Redirects like '2>&1', '1>&2', '&>', '>&' are
    self-contained (they redirect to/from file descriptors, not files).
    """
    # Bare redirects to files
    if tok in (">", ">>", "<", "<<", "<>"):
        return True
    # Numeric redirects: 2>/dev/null has a file target, 2>&1 does not
    if any(c.isdigit() for c in tok[:1]):
        i = 0
        while i < len(tok) and tok[i].isdigit():
            i += 1
        rest = tok[i:]
        if rest.startswith(">&"):
            # 2>&1 — redirect to file descriptor, no file target
            return False
        if rest == ">&":
            return False
        # 2>/dev/null — redirect to file
        if rest.startswith(">") or rest.startswith("<"):
            return True
    # &> / &>> redirect both stdout+stderr to a file
    if tok in ("&>", "&>>"):
        return True
    return False


def _strip_redirections(tokens: list[str]) -> list[str]:
    """Remove redirection tokens and their targets from a stage.

    Redirections like '2>&1' (fd-to-fd) have no file target and consume
    only themselves. Redirections like '>/dev/null', '<file', '2>file'
    consume the next token (the filename target). We strip both forms.
    """
    clean: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if _is_redirect_token(tok):
            i += 1
            # Only consume the next token if this redirect type has a file target
            if _redirect_has_file_target(tok):
                if i < len(tokens) and not _is_redirect_token(tokens[i]):
                    i += 1  # skip filename target
            continue
        clean.append(tok)
        i += 1
    return clean


def _split_stages(tokens: list[str]) -> list[list[str]]:
    """Split tokens into pipeline stages on separators (|, &&, ||, ;)."""
    stages: list[list[str]] = []
    current: list[str] = []
    for tok in tokens:
        if tok in _PIPELINE_SEPARATORS:
            if current:
                stages.append(current)
                current = []
        else:
            current.append(tok)
    if current:
        stages.append(current)
    return stages


# ---------------------------------------------------------------------------
# Consolidated read-only command whitelist.
#
# SOURCE OF TRUTH for the base list: bash_hook._WHITELIST_SINGLE and
# bash_hook._WHITELIST_COMPOUND. When those lists are updated, this list
# should be updated too. Pipeline-consumer additions are marked with [P].
# ---------------------------------------------------------------------------

# Commands eligible by name alone (any subcommand, as long as not excluded)
_READ_ONLY_SINGLE = frozenset({
    # === From bash_hook._WHITELIST_SINGLE ===
    "git", "pytest", "py.test", "jest", "vitest", "rspec", "ls", "find",
    "eslint", "flake8", "pylint", "shellcheck", "rubocop",
    "tail", "journalctl", "tree",
    "tsc", "webpack", "esbuild",
    "mocha", "karma", "tox", "nox", "ava", "gradle", "gradlew", "mvn",
    "deno", "bun",
    "sqlite3", "wc", "du", "df",
    "jq", "yq", "csvtool", "mlr", "csvcut",
    "gcloud", "aws", "az",
    "grep", "rg", "ag", "ack",
    # === [P] Pipeline consumers (read-only text filters/transformers) ===
    "head", "sort", "uniq", "cut", "tr", "column", "tee",
    "nl", "fmt", "fold", "paste", "join", "comm",
    "awk", "sed",
    # === [P] Additional read-only utilities safe in pipelines ===
    "cat", "echo", "printf", "xargs", "printenv",
    "which", "command", "type", "file", "stat",
    "dirname", "basename", "realpath", "readlink",
    "date", "env", "id", "whoami", "hostname",
    "pwd", "ps", "uptime", "uname",
    "true", "false", "test", "[",
    # === [P] Compression/decompression (read-only stdout) ===
    "gzip", "gunzip", "bzip2", "bunzip2", "xz", "unxz",
    "zcat", "bzcat", "xzcat",
    # === [P] Network diagnostic tools (read-only output) ===
    "ping", "traceroute", "nslookup", "dig", "host",
    "curl", "wget",  # with --head or -o /dev/null flags (not enforced here;
                     # worst case: output compressed, no security harm)
})

# Compound whitelist: (command, subcommand) pairs
_READ_ONLY_COMPOUND = frozenset({
    # === From bash_hook._WHITELIST_COMPOUND ===
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
    ("git", "branch"),
    ("python", "-m"), ("python3", "-m"),
    ("npx", "jest"), ("npx", "vitest"),
    ("npm", "test"),
    ("cargo", "test"),
    ("go", "test"),
    ("ruff", "check"),
    ("biome", "lint"),
    ("golangci-lint", "run"),
    ("docker", "pull"),
    ("pip", "list"), ("pip3", "list"),
    ("npm", "ls"),
    ("pnpm", "list"),
    ("docker", "ps"),
    ("brew", "list"),
    ("vite", "build"),
    ("next", "build"),
    ("go", "build"),
    ("cypress", "run"),
    ("playwright", "test"),
    ("npx", "cypress"),
    ("npx", "playwright"),
    ("npx", "mocha"),
    ("npx", "karma"),
    ("npx", "ava"),
    ("gradle", "test"),
    ("gradlew", "test"),
    ("mvn", "test"),
    ("deno", "test"),
    ("bun", "test"),
    ("docker", "logs"),
    ("docker", "inspect"),
    ("kubectl", "get"),
    ("kubectl", "describe"),
    ("kubectl", "logs"),
    ("kubectl", "top"),
    ("kubectl", "events"),
    # === [P] Additional read-only compound commands ===
    ("npm", "run"),
    ("pnpm", "run"),
    ("yarn", "run"),
    ("yarn", "test"),
    ("make", "-n"),  # dry-run only
    ("cargo", "check"),
    ("docker", "images"),
    ("docker", "info"),
    ("docker", "version"),
    ("kubectl", "explain"),
    ("kubectl", "api-resources"),
    ("kubectl", "api-versions"),
    ("kubectl", "config"),
    ("helm", "list"),
    ("helm", "status"),
    ("helm", "history"),
    ("helm", "get"),
    ("terraform", "plan"),
    ("terraform", "show"),
    ("terraform", "output"),
    ("terraform", "state"),
    ("git", "remote"),
    ("git", "config"),
    ("git", "blame"),
    ("git", "grep"),
    ("git", "ls-files"),
    ("git", "ls-tree"),
    ("git", "rev-parse"),
    ("git", "rev-list"),
    ("git", "describe"),
    ("git", "shortlog"),
    ("git", "stash"),
    # === [P] Read-only docker commands (no side effects) ===
    ("docker", "compose"),
})

# Git write subcommands (same as bash_hook._GIT_WRITE_SUBCMDS)
_GIT_WRITE_SUBCMDS = frozenset({
    "commit", "push", "pull", "merge", "rebase", "reset", "checkout",
    "switch", "stash", "tag", "cherry-pick", "revert", "am", "apply",
    "add", "rm", "mv", "restore", "bisect", "clean", "fetch", "clone",
    "init", "remote", "submodule", "worktree",
})

# Commands that are NEVER read-only, even if they appear in the whitelist
# (shell interpreters, privilege escalation, etc.)
_NEVER_READ_ONLY = frozenset({
    "bash", "sh", "zsh", "dash", "fish", "ksh",
    "sudo", "su", "doas", "pkexec",
    "python", "python3", "python2",  # interpreters: python -c is arbitrary code
    "node",  # node -e is arbitrary code
    "ruby", "perl", "php", "lua",
    "eval", "exec", "source",
})

# Commands excluded from git branch: `git branch -d` is a delete
# `git branch` without args is read-only listing; with -d/-D it's a write.
# The _READ_ONLY_COMPOUND includes ("git", "branch") for listing; we also
# allow it in _READ_ONLY_SINGLE with the constraint that the subcommand is
# "branch" (checked below).


def _is_stage_read_only(tokens: list[str]) -> tuple[bool, str]:
    """Check if a single pipeline stage is read-only.

    Args:
        tokens: The shlex-split tokens for this stage, with redirections
                already stripped.

    Returns:
        (is_read_only, reason) — reason is a short string for diagnostics.
    """
    if not tokens:
        return False, "empty-stage"

    # Strip leading env var assignments (FOO=bar)
    cmd_start = 0
    while cmd_start < len(tokens) and "=" in tokens[cmd_start] and not tokens[cmd_start].startswith("-"):
        cmd_start += 1

    if cmd_start >= len(tokens):
        return False, "env-only-stage"

    cmd = tokens[cmd_start]
    subcmd = tokens[cmd_start + 1] if cmd_start + 1 < len(tokens) else ""

    # Hard block: interpreters and privilege escalation
    if cmd in _NEVER_READ_ONLY:
        # Exception: python -m pytest, python3 -m <known-module>
        if cmd in ("python", "python3", "python2"):
            if subcmd == "-m" and cmd_start + 2 < len(tokens):
                module = tokens[cmd_start + 2]
                # Only allow known read-only modules
                if module in ("pytest", "unittest", "json.tool", "pip"):
                    return True, "python-m-whitelisted-module"
            return False, "python-not-whitelisted"
        if cmd == "node":
            if subcmd == "-e":
                return False, "node-e-arbitrary-code"
            # node with a script: check if it's piping JSON (read-only pattern)
            remaining = tokens[cmd_start + 1:]
            if any(arg.endswith(".json") or ".json" in arg for arg in remaining):
                return True, "node-json-inspection"
            return False, "node-not-whitelisted"
        return False, f"never-read-only:{cmd}"

    # Check compound whitelist (more specific first)
    if (cmd, subcmd) in _READ_ONLY_COMPOUND:
        # Git write subcommands are excluded even if in compound list
        if cmd == "git" and subcmd in _GIT_WRITE_SUBCMDS:
            return False, "git-write-subcmd"
        # kubectl secrets exclusion
        if cmd == "kubectl":
            remaining = tokens[cmd_start + 2:]
            if any(arg in ("secret", "secrets") or arg.startswith(("secret/", "secrets/")) for arg in remaining):
                return False, "kubectl-secrets"
        # docker compose: sub-subcommand check
        if cmd == "docker" and subcmd == "compose":
            subsub = tokens[cmd_start + 2] if cmd_start + 2 < len(tokens) else ""
            # docker compose ps/logs/config are read-only
            if subsub in ("ps", "logs", "config", "images", "version", "top", "events"):
                return True, "docker-compose-read-only"
            # docker compose up/down/build/run have side effects
            if subsub in ("up", "down", "build", "run", "exec", "pull", "push", "restart", "start", "stop", "rm"):
                return False, "docker-compose-side-effect"
            return False, "docker-compose-unknown"
        return True, "compound-whitelist"

    # Check single whitelist
    if cmd in _READ_ONLY_SINGLE:
        # Git: must have a non-write subcommand
        if cmd == "git":
            if not subcmd:
                return False, "git-no-subcmd"
            if subcmd in _GIT_WRITE_SUBCMDS:
                return False, "git-write-subcmd"
            return True, "git-read-only"
        # sqlite3: must not mutate. The -readonly flag opens in read-only mode.
        if cmd == "sqlite3":
            tokens_lower = [t.lower() for t in tokens[cmd_start:]]
            if any(w in tokens_lower for w in ("insert", "update", "delete", "drop", "alter", "create")):
                return False, "sqlite3-mutation"
            # Dot-commands (.dump, .schema, etc.) are read-only
            return True, "sqlite3-read-only"
        # sed: -i means in-place edit (side effect)
        if cmd == "sed":
            remaining = tokens[cmd_start + 1:]
            for arg in remaining:
                if arg == "-i" or (arg.startswith("-") and "i" in arg.replace("-", "")):
                    return False, "sed-in-place"
            return True, "sed-read-only"
        # curl/wget: these can be used to post/put data. We allow them
        # because the worst case is compressed output (no double-execution
        # risk since we're in PostToolUse). The user already ran the command.
        if cmd in ("curl", "wget"):
            return True, "network-fetch"
        # xargs: the subcommand determines safety
        if cmd == "xargs":
            # xargs with a read-only subcommand is fine; without subcommand
            # it defaults to echo (read-only)
            return True, "xargs"
        # tee: writes to files but in a pipeline it's typically used for
        # splitting output. Already ran; compressing output is safe.
        if cmd == "tee":
            return True, "tee"
        # docker: when in _READ_ONLY_SINGLE, it's via docker pull (compound)
        # or other read-only docker commands
        return True, "single-whitelist"

    # Not in any whitelist
    return False, f"not-whitelisted:{cmd}"


def is_read_only_pipeline(command_str: str) -> tuple[bool, str]:
    """Check if a shell command is read-only in all pipeline stages.

    Parses the command string, splits into stages on pipeline operators
    (|, &&, ||, ;), strips redirections from each stage, and checks every
    stage against the consolidated read-only whitelist.

    Args:
        command_str: The raw shell command string from tool_input.command.

    Returns:
        (is_read_only, reason) tuple. When is_read_only is True, the
        command is safe for output compression. When False, `reason`
        provides a short diagnostic.
    """
    if not command_str or not isinstance(command_str, str):
        return False, "empty-or-non-string"

    if command_str.strip() == "":
        return False, "whitespace-only"

    # Parse the command into tokens
    try:
        tokens = shlex.split(command_str, comments=True)
    except ValueError:
        return False, "unparseable-quoting"

    if not tokens:
        return False, "empty-after-split"

    # Split into stages
    stages = _split_stages(tokens)

    if not stages:
        return False, "no-stages"

    # Check each stage
    for i, stage_tokens in enumerate(stages):
        clean = _strip_redirections(stage_tokens)
        if not clean:
            continue  # empty after redirection stripping (all redirections)
        ok, reason = _is_stage_read_only(clean)
        if not ok:
            return False, f"stage-{i + 1}:{reason}"

    return True, "all-stages-read-only"


def get_pipeline_eligibility(command_str: str) -> dict:
    """Diagnostic function: return detailed eligibility information.

    Returns a dict with:
        is_eligible: bool
        reason: str
        stage_count: int
        stages: list of dicts with stage tokens, cleaned tokens, and eligibility
    """
    result: dict = {
        "is_eligible": False,
        "reason": "",
        "stage_count": 0,
        "stages": [],
    }

    if not command_str or command_str.strip() == "":
        result["reason"] = "empty"
        return result

    try:
        tokens = shlex.split(command_str, comments=True)
    except ValueError:
        result["reason"] = "unparseable"
        return result

    if not tokens:
        result["reason"] = "empty-after-split"
        return result

    stages = _split_stages(tokens)
    result["stage_count"] = len(stages)

    all_read_only = True
    for i, stage_tokens in enumerate(stages):
        clean = _strip_redirections(stage_tokens)
        stage_info = {
            "index": i,
            "raw_tokens": stage_tokens,
            "cleaned_tokens": clean,
            "is_read_only": True,
            "reason": "",
        }
        if not clean:
            stage_info["is_read_only"] = True  # empty stage is OK
            stage_info["reason"] = "empty-after-redirect-strip"
        else:
            ok, reason = _is_stage_read_only(clean)
            stage_info["is_read_only"] = ok
            stage_info["reason"] = reason
            if not ok:
                all_read_only = False

        result["stages"].append(stage_info)

    if all_read_only:
        result["is_eligible"] = True
        result["reason"] = "all-stages-read-only"
    else:
        # Find the first failing stage for the reason
        for s in result["stages"]:
            if not s["is_read_only"]:
                result["reason"] = f"stage-{s['index'] + 1}:{s['reason']}"
                break

    return result
