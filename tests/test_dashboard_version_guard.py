"""Version-downgrade guard: an OLDER Token Optimizer build must never overwrite a
dashboard a NEWER build already wrote.

Regression this prevents: a fix ships, but a stale long-lived daemon or a still-
running pre-upgrade session keeps regenerating the shared dashboard with pre-fix
code, clobbering the corrected file -- "we fixed it but it's still broken".
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "skills" / "token-optimizer" / "scripts"
sys.path.insert(0, str(SCRIPTS))

import measure  # noqa: E402


# --- _parse_semver ---------------------------------------------------------- #

def test_parse_semver_basic():
    assert measure._parse_semver("5.11.108") == (5, 11, 108)
    assert measure._parse_semver("v5.11.108") == (5, 11, 108)      # leading v
    assert measure._parse_semver("5.11") == (5, 11, 0)             # padded
    assert measure._parse_semver("5") == (5, 0, 0)
    assert measure._parse_semver("5.11.108-rc1") == (5, 11, 108)   # pre-release stripped
    assert measure._parse_semver("5.11.108+build") == (5, 11, 108) # build metadata stripped


def test_parse_semver_unparseable_is_none():
    # Real inputs are always strings (JSON meta / VERSION constant). These cannot
    # yield a numeric (major, minor, patch), so they must fail closed to None.
    for bad in ("", None, "dev", "abc.def", "x.y.z", "latest"):
        assert measure._parse_semver(bad) is None, bad


def test_semver_ordering():
    assert measure._parse_semver("5.11.104") < measure._parse_semver("5.11.106")
    assert measure._parse_semver("5.11.106") < measure._parse_semver("5.12.0")
    assert measure._parse_semver("5.11.106") == measure._parse_semver("5.11.106")


# --- _dashboard_on_disk_is_newer -------------------------------------------- #

# A real dashboard is 4-5MB; the guard ignores anything under the integrity floor.
# Tests that want the version logic exercised must write a >floor HTML.
_BIG_HTML = "<html>" + ("x" * (measure._DASHBOARD_MIN_VALID_BYTES + 100)) + "</html>"


def _write_meta(html_path, version):
    meta = measure._dashboard_meta_path(html_path)
    meta.write_text(json.dumps({"version": version, "shape": measure._DASHBOARD_SHAPE_MARKER}))


def test_on_disk_newer_blocks(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    _write_meta(html, "5.11.108")           # on disk is NEWER than me
    assert measure._dashboard_on_disk_is_newer(html) is True


def test_on_disk_older_allows(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.108")
    _write_meta(html, "5.11.104")           # on disk is OLDER
    assert measure._dashboard_on_disk_is_newer(html) is False


def test_on_disk_equal_allows(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.108")
    _write_meta(html, "5.11.108")           # equal is NOT newer -> allow
    assert measure._dashboard_on_disk_is_newer(html) is False


def test_fail_open_no_meta(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    # no sidecar written -> fail open (allow)
    assert measure._dashboard_on_disk_is_newer(html) is False


def test_fail_open_malformed_meta(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    measure._dashboard_meta_path(html).write_text("{not json")
    assert measure._dashboard_on_disk_is_newer(html) is False


def test_fail_open_unparseable_versions(tmp_path, monkeypatch):
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    # dev version on our side -> cannot compare -> allow
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "dev")
    _write_meta(html, "5.11.108")
    assert measure._dashboard_on_disk_is_newer(html) is False
    # unparseable version in the sidecar -> allow
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    _write_meta(html, "garbage")
    assert measure._dashboard_on_disk_is_newer(html) is False


# --- generate_standalone_dashboard early-skip ------------------------------- #

def test_generate_skips_write_when_on_disk_is_newer(tmp_path, monkeypatch):
    """The chokepoint: an older build calling generate_standalone_dashboard must
    return early WITHOUT overwriting a newer on-disk dashboard."""
    html = tmp_path / "dashboard.html"
    sentinel = "<html>NEWER-DASHBOARD-DO-NOT-CLOBBER" + ("x" * (measure._DASHBOARD_MIN_VALID_BYTES + 100)) + "</html>"
    html.write_text(sentinel)
    monkeypatch.setattr(measure, "DASHBOARD_PATH", html)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    _write_meta(html, "5.11.108")           # on disk written by a NEWER build

    result = measure.generate_standalone_dashboard(quiet=True, force=True)

    # returned the existing path and left the newer content untouched
    assert result == str(html)
    assert html.read_text() == sentinel


# --- HTML integrity floor (anti-wedge) -------------------------------------- #

def test_tiny_or_missing_html_fails_open_even_with_newer_sidecar(tmp_path, monkeypatch):
    """Anti-wedge: a newer sidecar next to a MISSING or tiny/corrupt HTML must NOT
    block writes -- otherwise a lying/corrupt sidecar wedges regeneration forever."""
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    html = tmp_path / "dashboard.html"
    _write_meta(html, "999.999.999")            # lying, impossibly-high version
    # (a) HTML missing entirely
    assert measure._dashboard_on_disk_is_newer(html) is False
    # (b) HTML present but below the integrity floor
    html.write_text("<html>truncated</html>")
    assert measure._dashboard_on_disk_is_newer(html) is False
    # (c) once the HTML is a real size, the newer sidecar is honored again
    html.write_text(_BIG_HTML)
    assert measure._dashboard_on_disk_is_newer(html) is True


# --- _dashboard_meta_is_fresh (SessionStart staleness decision) ------------- #

def test_meta_is_fresh_newer_on_disk_is_left_alone(monkeypatch):
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    # strictly-newer on disk -> fresh (older session must not regenerate it)
    assert measure._dashboard_meta_is_fresh(
        {"version": "5.11.108", "shape": measure._DASHBOARD_SHAPE_MARKER}) is True
    # even if the shape marker differs, a newer build owns it
    assert measure._dashboard_meta_is_fresh(
        {"version": "5.11.108", "shape": "OLD_SHAPE"}) is True


def test_meta_is_fresh_upgrade_and_shape_change_regenerate(monkeypatch):
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.108")
    # older on disk -> stale (I'm an upgrade; regenerate)
    assert measure._dashboard_meta_is_fresh(
        {"version": "5.11.104", "shape": measure._DASHBOARD_SHAPE_MARKER}) is False
    # same version, changed shape -> stale (regenerate)
    assert measure._dashboard_meta_is_fresh(
        {"version": "5.11.108", "shape": "OLD_SHAPE"}) is False
    # exact match -> fresh
    assert measure._dashboard_meta_is_fresh(
        {"version": "5.11.108", "shape": measure._DASHBOARD_SHAPE_MARKER}) is True
    # junk -> stale
    assert measure._dashboard_meta_is_fresh("nope") is False
    assert measure._dashboard_meta_is_fresh({}) is False


# --- _write_dashboard_meta -------------------------------------------------- #

def test_write_dashboard_meta_names_this_build(tmp_path, monkeypatch):
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.108")
    html = tmp_path / "dashboard.html"
    html.write_text(_BIG_HTML)
    measure._write_dashboard_meta(html)
    meta = json.loads(measure._dashboard_meta_path(html).read_text())
    assert meta["version"] == "5.11.108"
    assert meta["shape"] == measure._DASHBOARD_SHAPE_MARKER
    # and now an older build sees it as newer (guard would hold)
    monkeypatch.setattr(measure, "TOKEN_OPTIMIZER_VERSION", "5.11.106")
    assert measure._dashboard_on_disk_is_newer(html) is True
