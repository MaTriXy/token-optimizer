#!/usr/bin/env python3
"""Tests for the Cowork telemetry collector (cowork/collector/to_collector.py).

Covers the data-correctness fixes from the fable council review, all exercised
through the public `--db <tmp>` seam against a temp DB and synthetic OTLP
capture files. The real trends.db is never touched.
"""

import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
COLLECTOR = REPO / "cowork" / "collector" / "to_collector.py"
MEASURE = REPO / "skills" / "token-optimizer" / "scripts" / "measure.py"

# Force a known non-UTC zone so the UTC-vs-local day-bucketing test is
# meaningful. Restored after this module so the process TZ doesn't leak into
# other test files when the whole suite runs in one pytest process.
_ORIG_TZ = os.environ.get("TZ")
os.environ["TZ"] = "America/Los_Angeles"
if hasattr(time, "tzset"):
    time.tzset()


@pytest.fixture(scope="module", autouse=True)
def _restore_tz():
    yield
    if _ORIG_TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _ORIG_TZ
    if hasattr(time, "tzset"):
        time.tzset()


def _load(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load("to_collector_under_test", COLLECTOR)
measure = _load("measure_under_test", MEASURE)

PRICED_MODEL = "claude-sonnet-4-20250514"


# --------------------------------------------------------------------------- #
# Synthetic OTLP capture builders (mirror what CollectorHandler writes).
# --------------------------------------------------------------------------- #

def _attr(key, value):
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, float):
        return {"key": key, "value": {"doubleValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": str(value)}}


def _log_record(session_id, event_name, ts_nano, **attrs):
    a = [_attr("event.name", event_name), _attr("session.id", session_id)]
    a.extend(_attr(k, v) for k, v in attrs.items())
    return {"timeUnixNano": str(ts_nano), "attributes": a}


def _api_record(session_id, ts_nano, model=PRICED_MODEL, input_tokens=1000,
                output_tokens=500, cache_read_tokens=200, cache_creation_tokens=100,
                **extra):
    return _log_record(
        session_id, "claude_code.api_request", ts_nano, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens, **extra,
    )


def _write_capture(data_dir: Path, log_records, name="otlp-logs.jsonl"):
    data_dir.mkdir(parents=True, exist_ok=True)
    line = {
        "ts": "2026-08-14T02:30:00Z",
        "path": "/v1/logs",
        "content_type": "application/json",
        "kind": "json",
        "body": {"resourceLogs": [{"scopeLogs": [{"logRecords": list(log_records)}]}]},
    }
    with (data_dir / name).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _nanos(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


# A fixed UTC instant that is the previous local day in America/Los_Angeles:
# 2026-08-14 02:30 UTC == 2026-08-13 19:30 PDT.
EVENING_UTC = datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc)
EVENING_NANOS = _nanos(EVENING_UTC)
LOCAL_DAY = EVENING_UTC.astimezone().strftime("%Y-%m-%d")   # 2026-08-13
UTC_DAY = EVENING_UTC.strftime("%Y-%m-%d")                  # 2026-08-14


def _ingest(data_dir, db):
    return tc.ingest(data_dir, measure_path=str(MEASURE), db_override=str(db), quiet=True)


def _rows(db, where="platform='cowork'"):
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            f"SELECT jsonl_path, date, input_tokens, output_tokens, cost_usd, "
            f"cost_source, quality_score, quality_grade, cache_create_5m_tokens, "
            f"session_uuid FROM session_log WHERE {where}"
        ).fetchall()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #

def test_wellformed_ingest_lands_rows_and_derives_cost(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [
        _api_record("sess-alpha", EVENING_NANOS),
        _api_record("sess-alpha", EVENING_NANOS + 1_000_000),
    ])
    db = tmp_path / "trends.db"
    rc = _ingest(dd, db)
    assert rc == 0
    rows = _rows(db)
    assert len(rows) == 1
    row = rows[0]
    assert row[0] == "cowork:sess-alpha"
    # two api_requests * (1000 fresh + 200 cache_read + 100 cache_create) input
    assert row[2] == 2 * (1000 + 200 + 100)
    assert row[3] == 2 * 500
    assert row[4] > 0            # cost derived from priced model
    assert row[5] == "otel_derived"
    # cache_create attributed to the 5m column, not hardcoded 0
    assert row[8] == 2 * 100


def test_help_and_costview_run_without_traceback(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-cv", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    # --cost-view path (also exercises the Path.as_uri() DB URI)
    assert tc.cost_view(measure_path=str(MEASURE), db_override=str(db),
                        days=0, as_json=True) == 0


def test_overflow_and_huge_int_row_is_skipped_not_fatal(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [
        _api_record("sess-good", EVENING_NANOS),
        # int(float("inf")) raises OverflowError; 2**64 dies at the SQLite bind.
        _api_record("sess-bad", EVENING_NANOS, input_tokens="inf",
                    cache_creation_tokens=str(2 ** 64)),
    ])
    db = tmp_path / "trends.db"
    rc = _ingest(dd, db)
    assert rc == 0                       # whole ingest did not abort
    paths = {r[0] for r in _rows(db)}
    assert "cowork:sess-good" in paths   # the good session still landed
    good = [r for r in _rows(db) if r[0] == "cowork:sess-good"][0]
    assert good[2] == 1000 + 200 + 100   # untouched by the bad row


def test_int_helper_clamps_out_of_range(tmp_path):
    assert tc._int(float("inf")) == 0
    assert tc._int("inf") == 0
    assert tc._int(2 ** 64) == 0
    assert tc._int(-(2 ** 64)) == 0
    assert tc._int("1000") == 1000
    assert tc._int(None) == 0


def test_quality_average_fold_is_not_nulled(tmp_path):
    db = tmp_path / "trends.db"
    # Pre-seed a Claude row on LOCAL_DAY with a real quality score.
    measure.TRENDS_DB = db
    measure.SNAPSHOT_DIR = db.parent
    conn = measure._init_trends_db()
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, platform, quality_score, "
        "quality_grade, input_tokens, output_tokens, session_uuid, cost_usd) "
        "VALUES (?, ?, 'claude', 82, 'B', 100, 50, 'claude-uuid-1', 0.0)",
        ("/home/u/.claude/projects/p/claude-uuid-1.jsonl", LOCAL_DAY),
    )
    conn.commit()
    conn.close()

    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-q", EVENING_NANOS)])
    assert _ingest(dd, db) == 0

    conn = sqlite3.connect(str(db))
    try:
        avg = conn.execute(
            "SELECT avg_quality_score FROM daily_stats WHERE date = ?", (LOCAL_DAY,)
        ).fetchone()
    finally:
        conn.close()
    assert avg is not None
    assert avg[0] is not None            # NOT NULLed by the cowork row
    assert avg[0] == pytest.approx((82 + 0) / 2)   # cowork folds in as 0, not NULL


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="needs time.tzset() to force a non-UTC local tz so the instant crosses "
    "local midnight; tzset is POSIX-only, so on Windows LOCAL_DAY==UTC_DAY and the "
    "cross-midnight scenario can't be constructed. Local-day bucketing itself is "
    "exercised on POSIX CI + a real Windows user's own tz.",
)
def test_local_day_bucketing(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-tz", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert LOCAL_DAY != UTC_DAY           # sanity: the instant really crosses midnight
    assert row[1] == LOCAL_DAY            # bucketed to local day, not UTC day


def test_event_dedup_on_duplicated_batch(tmp_path):
    dd = tmp_path / "cap"
    rec = _api_record("sess-dup", EVENING_NANOS)
    # Same event delivered twice (retry) plus a second rotated file with it again.
    _write_capture(dd, [rec, dict(rec)], name="otlp-logs.jsonl")
    _write_capture(dd, [dict(rec)], name="otlp-logs-1.jsonl")

    sessions, stats = tc.parse_cowork_sessions(dd)
    assert stats["duplicate_events"] == 2
    s = sessions["sess-dup"]
    assert s["api_calls"] == 1           # counted once despite three copies
    bd = s["breakdown"][PRICED_MODEL]
    assert bd["fresh_input"] == 1000

    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert row[2] == 1000 + 200 + 100    # single event's tokens, not tripled


def test_cross_platform_double_count_skip(tmp_path):
    db = tmp_path / "trends.db"
    # A Claude row already carries this session_uuid (TO's Stop hook fired in-VM).
    measure.TRENDS_DB = db
    measure.SNAPSHOT_DIR = db.parent
    conn = measure._init_trends_db()
    conn.execute(
        "INSERT INTO session_log (jsonl_path, date, platform, input_tokens, "
        "output_tokens, session_uuid, cost_usd) VALUES "
        "(?, ?, 'claude', 4242, 111, 'shared-sid', 0.99)",
        ("/home/u/.claude/projects/p/shared-sid.jsonl", LOCAL_DAY),
    )
    conn.commit()
    conn.close()

    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("shared-sid", EVENING_NANOS)])
    assert _ingest(dd, db) == 0
    # The cowork row must NOT have been written (would double-count the session).
    assert _rows(db, "jsonl_path='cowork:shared-sid'") == []
    # The original Claude row is untouched.
    claude = _rows(db, "session_uuid='shared-sid'")
    assert len(claude) == 1
    assert claude[0][0].endswith("shared-sid.jsonl")


def test_reported_cost_cumulative_is_rejected(tmp_path):
    dd = tmp_path / "cap"
    # A single call reports an absurd cumulative cost >> derived → reject reported.
    _write_capture(dd, [_api_record("sess-cost", EVENING_NANOS, cost_usd=999.0)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    row = _rows(db)[0]
    assert row[5] == "otel_derived"      # fell back to derived, not otel_reported
    assert row[4] < 1.0                  # derived, not the 999 cumulative figure


def test_user_version_stamped(tmp_path):
    dd = tmp_path / "cap"
    _write_capture(dd, [_api_record("sess-ver", EVENING_NANOS)])
    db = tmp_path / "trends.db"
    assert _ingest(dd, db) == 0
    conn = sqlite3.connect(str(db))
    try:
        ver = conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
    assert ver == 3


def test_oversized_body_replies_413():
    # Unit-level check of the 413 branch without binding a socket: drive do_POST
    # with a stub that captures the reply status.
    handler = tc.CollectorHandler.__new__(tc.CollectorHandler)
    handler.headers = {"Content-Length": str(tc.MAX_BODY + 1)}
    handler.path = "/v1/logs"
    captured = {}

    def fake_reply(status, body):
        captured["status"] = status
        captured["body"] = body

    handler._reply = fake_reply
    handler.do_POST()
    assert captured["status"] == 413


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
