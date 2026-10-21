# -*- coding: utf-8 -*-
"""V2-4: HG HTTP collector stability tests (backoff, Retry-After, real payload parse).

Uses a fake session + the REAL captured XML snapshot, so parsing/upsert paths are
exercised without network access. A TEMP database is used (backend.db.DB_PATH
monkeypatched) — production data/football.db is untouched.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backend.db as dbmod  # noqa: E402

TMPDIR = tempfile.mkdtemp(prefix="fhg_http_")
dbmod.DB_PATH = Path(TMPDIR) / "test.db"
dbmod.init_db()

from backend.hg_http import HGHttpCollector  # noqa: E402

SAMPLE = ROOT / "_hg_game_list_sample.xml"
assert SAMPLE.exists(), "missing real XML snapshot"

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")


def make_collector(fake_session, interval: float = 0.1) -> HGHttpCollector:
    c = HGHttpCollector(interval=interval)
    c._session = fake_session
    c._bootstrapped = True
    c._post_template = "p=get_game_list&uid=t_uid&ver=t_ver&ts=0"
    return c


def test_429_backoff_with_retry_after():
    print("== 429 限流：按 Retry-After 退避（下限 10s） ==")
    calls = {"n": 0}

    class FakeResp:
        status_code = 429
        headers = {"Retry-After": "25"}
        text = "rate limited"

    class FakeSession:
        def post(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    c = make_collector(FakeSession())
    t0 = time.time()
    c._poll_once()
    check("one request fired", calls["n"] == 1)
    check("backoff_until set >= +10s", c._backoff_until - t0 >= 10.0)
    check("backoff_until honored Retry-After(25)", 24.0 <= c._backoff_until - t0 <= 26.0,
          f"{(c._backoff_until - t0):.1f}s")
    check("fail_count=1", c.fail_count == 1)
    check("last_error mentions 429", "429" in (c.last_error or ""))
    check("session not marked expired on 429", c.session_expired is False)


def test_5xx_backoff():
    print("== 5xx 服务端错误：退避 10s ==")
    calls = {"n": 0}

    class FakeResp:
        status_code = 503
        headers = {}
        text = "busy"

    class FakeSession:
        def post(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    c = make_collector(FakeSession())
    t0 = time.time()
    c._poll_once()
    check("one request fired", calls["n"] == 1)
    check("backoff_until ~ +10s", 9.0 <= c._backoff_until - t0 <= 11.0,
          f"{(c._backoff_until - t0):.1f}s")


def test_bad_response_forces_rebootstrap():
    print("== 会话失效响应（table id error / VariableStandard）→ 强制重 bootstrap ==")
    calls = {"n": 0}

    class FakeResp:
        status_code = 200
        headers = {}
        text = "table id error"

    class FakeSession:
        def post(self, *a, **k):
            calls["n"] += 1
            return FakeResp()

    c = make_collector(FakeSession())
    c._poll_once()
    check("bootstrapped reset to False", c._bootstrapped is False)
    check("session_expired=True", c.session_expired is True)


def test_real_payload_parse_and_upsert():
    print("== 真实 XML 快照：解析 → 入库 → 快照去重 ==")
    xml_text = SAMPLE.read_text(encoding="utf-8")

    class FakeResp:
        status_code = 200
        headers = {}
        text = xml_text

    class FakeSession:
        def __init__(self):
            self.calls = 0

        def post(self, *a, **k):
            self.calls += 1
            return FakeResp()

    fs = FakeSession()
    c = make_collector(fs)
    c._poll_once()
    check("success_count=1", c.success_count == 1)
    check("matches_open>0", c.matches_open > 0, f"open={c.matches_open}")
    check("last_valid_event_at set", c.last_valid_event_at is not None)
    check("request/parse/db latencies recorded",
          bool(c._req_ms) and bool(c._parse_ms) and bool(c._db_ms))
    # upsert went to temp DB
    from backend.db import fetchone
    row = fetchone("SELECT COUNT(*) c FROM hg_live_matches")
    n1 = int(row["c"])
    check("hg_live_matches rows>0", n1 > 0, f"rows={n1}")
    # second identical poll → no new rows, no change events
    c._poll_once()
    row2 = fetchone("SELECT COUNT(*) c FROM hg_live_matches")
    check("dedup: row count unchanged", int(row2["c"]) == n1,
          f"{n1} -> {int(row2['c'])}")
    check("change_count=0 for identical snapshot", c.change_count == 0, str(c.change_count))
    check("canonical sync report present", isinstance(c.last_sync_report, dict),
          str(c.last_sync_report))


if __name__ == "__main__":
    test_429_backoff_with_retry_after()
    test_5xx_backoff()
    test_bad_response_forces_rebootstrap()
    test_real_payload_parse_and_upsert()
    print(f"\n=== PASS={PASS} FAIL={FAIL} ===")
    sys.exit(1 if FAIL else 0)
