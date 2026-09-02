# Local extensions (post-flush hook)

Token Optimizer's session-end flush worker can run one optional **local
extension** after its own work completes. This is a power-user/admin feature:
Token Optimizer ships no extensions, runs none by default, and takes no
responsibility for third-party ones.

## How it works

- Location: a single file at `<config dir>/extensions/post_flush.py`
  (e.g. `~/.claude/token-optimizer/extensions/post_flush.py` on Claude Code).
- Off by default: with no file present, nothing is loaded and nothing runs.
- Contract: the file defines `run(context)`. `context` is a dict with:
  - `trends_db` — path to the local `trends.db`
  - `snapshot_dir` — the local snapshot dir
  - `config_dir` — the config dir
  - `runtime` — detected runtime name
  - `version` — Token Optimizer version
  - `time_left_fn` — callable returning seconds left in the flush budget;
    defer work that does not fit, the worker may be hard-exited at 20s.
- Fail-open: any exception the extension raises is swallowed; the hook always
  completes and the flush lock is always released.
- Safety: the file is loaded only from that exact path (never from the repo,
  the snapshot dir, or an environment override) and only when it is safe to
  execute — owned by the current user, not group/world-writable, and with
  extensions/config directories that are not group/world-writable either (a
  peer able to write the file or its parent directory could swap in code). On
  Windows these checks are not enforced (no POSIX uid and no group/other
  permission split); the config directory's own ACLs are the protection there.
  Extensions run with your user's privileges — only install code you trust.

## Example

```python
# extensions/post_flush.py
def run(context):
    left = context["time_left_fn"]()
    if left < 5:
        return  # not enough budget; do it next flush
    # ... your local post-flush work ...
```
