# UNIT B: Expand Bash-Compression Eligibility via PostToolUse Output Compression

## Problem Statement

Token Optimizer v5.12.4's bash compression (the largest token-saving lever) fires on only
**1.3%** of real terminal-agent commands. The root cause is categorical rejection of shell
metacharacters in `bash_hook.py`:

- 140 of 153 (91.5%) real commands excluded for metachars: pipes, `&&`, `2>&1`, heredocs
- 11 of 153 (7.2%) excluded for non-whitelisted binaries
- Only 2 of 153 (1.3%) eligible

The PreToolUse command-rewriting architecture forces this exclusion: any metachar in the
command makes safe reconstruction impossible without a full shell parser.

## Approach Evaluation

| Approach | Verdict | Rationale |
|----------|---------|-----------|
| **(a) PostToolUse `updatedToolOutput`** | **CHOSEN** | Officially supported by Claude Code v2.1+. No command rewriting — compress output that already ran. Eliminates the metachar problem entirely. |
| (b) Safe raw-exec wrapper | Rejected | Even with pipeline-aware read-only parsing, double-execution of side-effecting commands is an unacceptable risk. A `git push` inside a pipeline would run twice. |
| (c) Hybrid | Rejected | Adds complexity without benefit. Once PostToolUse output compression works, the wrapper approach is unnecessary. |

## Chosen Approach: PostToolUse `updatedToolOutput`

### Evidence of Viability

Claude Code hooks documentation (code.claude.com/docs/en/hooks):

> **PostToolUse decision control**: `updatedToolOutput` replaces the tool's output with
> the provided value before it is sent to Claude. The value must match the tool's output
> shape.

Example from the official docs:
```json
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "updatedToolOutput": {
      "stdout": "[redacted]",
      "stderr": "",
      "interrupted": false,
      "isImage": false
    }
  }
}
```

The Bash tool's output shape is `{stdout, stderr, interrupted, isImage}`. We receive the
full `tool_response` containing both `tool_input.command` and `tool_response.{stdout, stderr,
...}`, apply compression to stdout, and return `updatedToolOutput` with the compressed stdout
while preserving stderr unchanged.

The existing codebase already uses this mechanism: `archive_result.py` returns
`updatedMCPToolOutput` for MCP tools in PostToolUse. We extend the pattern to Bash.

### Architecture

```
Before (PreToolUse rewrite, current):
  CC → bash_hook.py (PreToolUse)
       → refuse metachars → pass through (1.3% eligible)
       → match whitelist   → rewrite command → re-execute through bash_compress.py

After (PostToolUse compression, new):
  CC → Bash tool runs normally (NO rewrite)
     → bash_compress_hook.py (PostToolUse)
       → receive tool_response (already-executed output)
       → analyze command for read-only pipeline safety
       → if read-only: compress stdout via existing bash_compress.compress()
       → return updatedToolOutput with compressed stdout
       → if side-effecting or failed: pass through raw
```

Key benefits:
1. **No command rewriting** — eliminates the metachar problem entirely
2. **No double execution** — the command already ran, we just compress its output
3. **Reuses existing compression** — same `bash_compress.compress()` function, same handlers
4. **Same safety guarantees** — fail-open, token preservation scan, raw output archiving
5. **Pipeline-aware eligibility** — we can now compress `git log | head`, `cat f | grep x`,
   `cmd 2>&1 | tail`, `find . -name '*.py' | xargs grep TODO`, etc.

### Hook Configuration

Add a new PostToolUse hook for Bash in `hooks/hooks.json`:

```json
{
  "matcher": "Bash",
  "hooks": [{
    "type": "command",
    "command": "... python-launcher.sh ... bash_compress_hook.py --quiet",
    "timeout": 15
  }]
}
```

The existing PreToolUse hook (`bash_hook.py`) remains in place as a fallback — it continues
to handle the 1.3% of commands that pass the current metachar-free whitelist. The new
PostToolUse hook processes the REST (the 98.7% that currently pass through uncompressed).

### New Eligibility Rules

The existing PreToolUse eligibility (metachar-free + whitelist) stays unchanged.

The NEW PostToolUse eligibility for pipeline/metachar commands:

1. **Always pass through raw** (no compression) when:
   - Command failed (non-zero exit code)
   - Error patterns on stderr (same as existing `_looks_like_failure`)
   - Output is too small (<100 chars, same as existing gate)
   - The output is an image (`isImage: true`)
   - The tool was interrupted

2. **Pipeline read-only check**: Parse the command string into stages using shell-aware
   tokenization. Each stage is the command between pipeline operators (`|`, `|&`),
   list separators (`&&`, `||`, `;`), and redirections are stripped before analysis.
   A command is eligible for compression ONLY when EVERY stage is a known read-only
   command. The analysis is conservative: any unrecognized stage → pass through raw.

3. **Expanded read-only whitelist** for pipeline stages (adds to existing whitelist):
   - Pipeline consumers: `head`, `tail`, `wc`, `sort`, `uniq`, `cut`, `tr`, `sed` (no `-i`),
     `awk`, `tee`, `column`
   - Text filters: `grep`, `rg`, `ag`, `ack` (already whitelisted)
   - Count/math: `expr`
   - Environment: `printenv`, `which`, `command`, `type`
   - File info: `file`, `stat`, `du`, `df` (already whitelisted)
   - Read-only `cat` (on existing files, no redirect output)
   - Read-only `echo` (no redirect to file)
   - Read-only `xargs` with read-only subcommand
   - All existing whitelist entries (`git log`, `find`, `ls`, etc.)

4. **Side-effecting commands ALWAYS pass through raw**:
   - Write operations: `git push/commit/add/rm`, `rm`, `mv`, `cp`, `mkdir`, `touch`,
     `chmod`, `chown`, `npm install`, `pip install`, `docker build/push/run`, etc.
   - Destructive operations: `kill`, `pkill`, `shutdown`, `reboot`
   - Interpreters with arbitrary code: `python -c`, `node -e`, `bash -c`, `sh -c`
   - Package managers with side effects: `brew install`, `apt-get`, `yum`, `cargo install`

5. **Injection safety**: The PostToolUse hook receives the ALREADY-EXECUTED command in
   `tool_input.command` and the output in `tool_response`. No command reconstruction,
   no `shell=True`, no re-execution. The only new risk is incorrect read-only
   classification — mitigated by the fail-open default (unknown = pass through raw).

### Safety Argument

1. **Read-only only**: Pipeline analysis ensures only commands where ALL stages are
   read-only get compressed. Any unrecognized stage → raw passthrough.

2. **No double side-effects**: The command already ran. We never re-execute. We only
   read `tool_response` (already captured) and potentially replace what Claude sees.
   The disk/networking effects already happened — we can't undo them, but we also
   can't cause new ones.

3. **Injection-safe**: No `shell=True`, no string interpolation into a shell command.
   The PostToolUse hook receives structured JSON from Claude Code and returns
   structured JSON. The compression function operates on the already-captured stdout
   string.

4. **Fail-open**: Any exception in the hook → exit 0 with no output → Claude sees the
   original uncompressed result. The hook never blocks, never errors out.

5. **Token preservation**: The same PRE-compression credential scan from
   `bash_compress.py` runs before compression. Credential-bearing lines are
   re-injected after compression.

6. **Raw output archived**: The full uncompressed stdout/stderr is archived to disk
   via the existing `archive_result.py` infrastructure. Claude can retrieve it with
   `expand <key>`.

7. **No security regression**: The existing PreToolUse hook is unchanged. The new
   PostToolUse hook is additive and fail-open. If it breaks, the worst case is
   uncompressed output (same as today).

### Measurement Plan

**Corpus**: Build a corpus of 30+ realistic pipeline/metachar commands representing
the shapes that were previously excluded:

```
# Pipes (previously excluded by |)
git log --oneline | head -20
cat package.json | grep version
find src -name '*.py' | xargs grep TODO
ps aux | grep node | wc -l
grep -r "FIXME" src/ | sort | uniq -c | sort -rn | head -10

# Redirects (previously excluded by >)
python script.py 2>&1 | tail -30
npm test 2>&1 | grep -E "(passing|failing)"

# List operators (previously excluded by &&)
git fetch && git status
cd src && ls -la

# Heredocs (previously excluded by <<)
cat << 'EOF' | wc -l

# Command substitution (previously excluded by $)
echo "Node version: $(node --version)"
```

**Metrics**:
- `eligibility_rate`: fraction of commands where compression is attempted
- `compression_rate`: fraction of eligible commands where compression was applied
- `safety_violations`: count of side-effecting commands that were incorrectly classified as read-only (must be 0)
- `token_savings`: tokens saved per compressed command

**Target**: eligibility rate >50% on the test corpus (up from 1.3%), with ZERO safety
violations.

### Test Plan

1. **Unit test**: `test_posttooluse_read_only_pipeline_detection.py`
   - Test pipeline parser against the corpus
   - Assert all read-only pipelines pass eligibility
   - Assert all side-effecting pipelines are rejected
   - Assert edge cases (empty pipeline, unmatched quotes, etc.)

2. **Integration test**: `test_posttooluse_bash_compress_hook.py`
   - Run the new PostToolUse hook with mock tool_response payloads
   - Assert correct `updatedToolOutput` JSON structure
   - Assert compression is applied only to stdout
   - Assert stderr survives unchanged
   - Assert fail-open on exceptions

3. **Existing tests must stay green**:
   - `test_bash_hook_worktree_skip.py` (PreToolUse behavior unchanged)
   - `test_command_filters_toml.py` (whitelist/exclude logic unchanged)
   - All bash_compress.py handler tests

### Files to Create/Modify

| File | Action | Description |
|------|--------|-------------|
| `design/UNIT-B-DESIGN.md` | CREATE | This document |
| `skills/token-optimizer/scripts/pipeline_analyzer.py` | CREATE | Pipeline-aware read-only safety classifier |
| `skills/token-optimizer/scripts/bash_compress_hook.py` | CREATE | PostToolUse hook entry point |
| `hooks/hooks.json` | MODIFY | Add PostToolUse Bash hook entry |
| `tests/test_pipeline_analyzer.py` | CREATE | Pipeline analysis tests with corpus |
| `tests/test_posttooluse_bash_compress.py` | CREATE | Integration tests for the new hook |
| `cowork/decisions.md` | MODIFY | Append design decisions |

### Open Questions

1. **Should the existing PreToolUse `bash_hook.py` be removed or kept?**
   Kept as-is (no change to existing behavior). The PreToolUse hook handles the 1.3% of
   simple commands; the PostToolUse hook handles everything else. Future cleanup could
   fold both paths into PostToolUse-only.

2. **What happens if both PreToolUse rewrite AND PostToolUse compression fire?**
   PreToolUse rewrites the command (current behavior), and the rewritten command runs
   through bash_compress.py. PostToolUse then sees the ALREADY-compressed output from
   the wrapper. The PostToolUse hook should detect that the output is already compressed
   (or that the command was rewritten by bash_hook) and pass through. Alternatively,
   we can use the fact that archived results from PreToolUse carry a pointer suffix
   to detect this case.

3. **Should we compress output of `python script.py`?**
   Python scripts are NOT in the read-only whitelist, so they pass through raw. This
   is correct and conservative: a Python script could do anything. Only explicitly
   whitelisted Python invocations (like `python -m pytest`) are eligible.
