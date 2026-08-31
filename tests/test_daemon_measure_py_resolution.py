"""The daemon must never execute a measure.py it cannot vouch for.

An earlier version of _resolve_measure_py walked seven install roots with
followlinks=True and executed whichever copy self-reported the highest version
in its .claude-plugin/plugin.json. Any unrelated plugin shipping a nested
token-optimizer/scripts/measure.py plus a manifest claiming a huge version won
that contest, and a symlink under any walked root escaped the roots entirely.

Resolution is now confined to the version container of the identity that
generated the daemon, versions come from directory names, symlinks are skipped,
and the result is containment-checked.
"""
import os
import re
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

SUFFIX = os.path.join("skills", "token-optimizer", "scripts", "measure.py")


def _load_resolver(fallback: str, tmp_log: Path):
    """Exec just the resolver out of the generated daemon source.

    The resolver lives inside a generated template, so it cannot be imported.
    Extracting it keeps the test bound to the shipped text rather than a copy
    that could drift away from what users actually run.
    """
    import measure

    src = measure._generate_daemon_script()

    # issue #160: _resolve_measure_py now guards _MEASURE_PY_CACHE with
    # _STATE_LOCK; give the extracted function a real lock so it runs in-isolation.
    ns = {"os": os, "json": __import__("json"), "time": __import__("time"),
          "DASHBOARD": "",
          "_STATE_LOCK": threading.Lock()}
    for const in ["_MEASURE_PY_CACHE", "MEASURE_PY_RESOLVE_TTL", "MEASURE_PY_MARKETPLACE"]:
        m = re.search(r"^%s = .*$" % re.escape(const), src, re.M)
        assert m, f"{const} missing from generated daemon"
        exec(m.group(0), ns)
    ns["MEASURE_PY_FALLBACK"] = fallback
    ns["REGEN_LOG"] = str(tmp_log)
    for fn in ["_log_regen", "_resolve_measure_py"]:
        m = re.search(r"^def %s\(.*?\n(?=^\S)" % fn, src, re.M | re.S)
        assert m, f"{fn} missing from generated daemon"
        exec(m.group(0), ns)
    return ns["_resolve_measure_py"]


def _make_install(root: Path, identity: str, version: str) -> Path:
    """Create <root>/<identity>/token-optimizer/<version>/<SUFFIX>."""
    p = root / identity / "token-optimizer" / version / SUFFIX
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("# measure.py\n", encoding="utf-8")
    manifest = root / identity / "token-optimizer" / version / ".claude-plugin"
    manifest.mkdir(parents=True, exist_ok=True)
    (manifest / "plugin.json").write_text('{"version": "%s"}' % version, encoding="utf-8")
    return p


def test_resolves_newest_sibling_when_generating_version_was_pruned(tmp_path):
    """The original bug: the generating version is gone after an upgrade."""
    cache = tmp_path / "cache"
    gone = _make_install(cache, "alexgreensh-token-optimizer", "5.11.58")
    newest = _make_install(cache, "alexgreensh-token-optimizer", "5.11.65")
    _make_install(cache, "alexgreensh-token-optimizer", "5.11.63")
    import shutil
    shutil.rmtree(gone.parents[3])  # remove the 5.11.58 version dir entirely

    resolve = _load_resolver(str(gone), tmp_path / "log.txt")
    assert Path(resolve()).resolve() == newest.resolve()


def test_ignores_a_foreign_plugin_claiming_a_huge_version(tmp_path):
    """The P0: another plugin must never win the resolution and get executed."""
    cache = tmp_path / "cache"
    real = _make_install(cache, "alexgreensh-token-optimizer", "5.11.65")
    # A hostile neighbour under the SAME cache root, different identity,
    # advertising a version that would beat everything in a global contest.
    evil = _make_install(cache, "evil-marketplace", "999.0.0")

    resolve = _load_resolver(str(real), tmp_path / "log.txt")
    got = Path(resolve()).resolve()
    assert got == real.resolve(), f"resolver selected a foreign plugin: {got}"
    assert evil.resolve() != got


def test_ignores_a_symlinked_version_directory(tmp_path):
    """followlinks=True previously let a symlink escape the install roots."""
    cache = tmp_path / "cache"
    real = _make_install(cache, "alexgreensh-token-optimizer", "5.11.65")
    outside = tmp_path / "outside"
    outside_py = outside / SUFFIX
    outside_py.parent.mkdir(parents=True, exist_ok=True)
    outside_py.write_text("# attacker\n", encoding="utf-8")

    container = cache / "alexgreensh-token-optimizer" / "token-optimizer"
    link = container / "9.9.9"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")

    resolve = _load_resolver(str(real), tmp_path / "log.txt")
    got = Path(resolve()).resolve()
    assert got == real.resolve(), f"resolver followed a symlink out of the container: {got}"


def test_resolver_does_not_reintroduce_a_broad_root_walk():
    """Structural guard, because the behavioural tests above cannot catch this.

    The vulnerable resolver walked hardcoded absolute install roots, so a
    tmp_path fixture never intersected it and every behavioural assertion above
    passed vacuously against the broken code. Only an assertion about the
    shipped source text actually fails if someone reintroduces the wide walk.
    """
    import measure

    src = measure._generate_daemon_script()
    start = src.index("def _resolve_measure_py(")
    end = src.index("\ndef ", start + 1)
    # Strip comments and the docstring: they legitimately NAME the old vulnerable
    # constructs while explaining why they were removed, and matching those would
    # make this guard fire on its own documentation.
    raw = src[start:end]
    body = "\n".join(
        line.split("#", 1)[0] for line in raw.splitlines()
        if not line.strip().startswith("#")
    )
    if '"""' in body:
        head, _, rest = body.partition('"""')
        _doc, _, tail = rest.partition('"""')
        body = head + tail

    assert "followlinks=True" not in body, (
        "resolver follows symlinks again; a link under an install root can "
        "escape the container and be executed"
    )
    assert "os.walk(" not in body, (
        "resolver walks directories again; resolution must stay a flat listing "
        "of the generating identity's version container"
    )
    for root in ("~/.claude/skills", "~/.claude/plugins/cache", "~/.codex/skills",
                 "~/.config/opencode/plugins"):
        assert root not in body, f"resolver reaches outside its identity again: {root}"
    assert "plugin.json" not in body, (
        "resolver trusts a manifest version again; the version must come from "
        "the directory name, which another writer cannot forge in place"
    )
    assert "islink" in body and "realpath" in body, (
        "resolver lost its symlink skip or containment check"
    )


def test_falls_back_to_the_generated_path_when_nothing_resolves(tmp_path):
    """No container at all: keep the old behaviour rather than returning junk."""
    lonely = tmp_path / "solo" / "token-optimizer" / "5.11.65" / SUFFIX
    lonely.parent.mkdir(parents=True, exist_ok=True)
    lonely.write_text("# measure.py\n", encoding="utf-8")
    resolve = _load_resolver(str(lonely), tmp_path / "log.txt")
    assert Path(resolve()).resolve() == lonely.resolve()


def test_prerelease_version_does_not_rank_below_an_older_stable(tmp_path):
    """A pre-release dir must not be out-ranked by an older stable one.

    The first implementation filtered non-numeric components out, so
    "5.11.66-rc1" collapsed to (5, 11) and lost to (5, 11, 60). The daemon would
    then run an older build than the one installed.
    """
    cache = tmp_path / "cache"
    older = _make_install(cache, "alexgreensh-token-optimizer", "5.11.60")
    rc = _make_install(cache, "alexgreensh-token-optimizer", "5.11.66-rc1")

    resolve = _load_resolver(str(older), tmp_path / "log.txt")
    got = Path(resolve()).resolve()
    assert got == rc.resolve(), (
        f"resolver picked {got.parents[3].name} over the newer 5.11.66-rc1"
    )


def test_non_numeric_version_directories_are_skipped(tmp_path):
    """Names like 'latest' or 'dev' must not crash or win."""
    cache = tmp_path / "cache"
    real = _make_install(cache, "alexgreensh-token-optimizer", "5.11.65")
    _make_install(cache, "alexgreensh-token-optimizer", "latest")
    _make_install(cache, "alexgreensh-token-optimizer", "dev")

    resolve = _load_resolver(str(real), tmp_path / "log.txt")
    assert Path(resolve()).resolve() == real.resolve()
