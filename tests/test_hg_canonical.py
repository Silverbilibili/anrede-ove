# -*- coding: utf-8 -*-
"""V2-3/V2-6: HG↔TC canonical-model integration tests.

Independent assertions (no reuse of production output as expected value):
  * matched HG quote sync into canonical quotes
  * engine consumes TC+HG cross-source and only yields opportunities when
    the combined book is < 1 (no fake opportunities otherwise)
  * dedup: identical odds do not create new quote versions
  * stale / in-play / kickoff-mismatch / team-swap / alias / TC-empty rules
Uses a TEMPORARY database (backend.db.DB_PATH monkeypatched).
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from decimal import Decimal  # noqa: E402

import backend.db as dbmod  # noqa: E402
from backend.collector.collector import (  # noqa: E402
    upsert_normalized_quote, source_market_key,
)
from backend.engine.engine import authoritative_calculate  # noqa: E402

TMPDIR = tempfile.mkdtemp(prefix="fhg_test_")
dbmod.DB_PATH = Path(TMPDIR) / "test.db"
dbmod.init_db()

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


def now_iso(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def seed_tc_match(mid: str = "M_TC_0001", home: str = "Manchester United",
                  away: str = "Chelsea", odds: tuple = ("2.10", "3.20", "3.50")) -> str:
    n = {
        "source": "TC", "source_match_id": "tcm1", "competition": "EPL",
        "home_team": home, "away_team": away, "kickoff_time_utc": now_iso(),
        "match_id": mid, "market_id": source_market_key(mid, "MATCH_RESULT", None),
        "market_type": "MATCH_RESULT", "line": None,
        "selections": [
            {"code": "HOME", "display_name": "主胜", "odds": odds[0], "status": "OPEN"},
            {"code": "DRAW", "display_name": "平局", "odds": odds[1], "status": "OPEN"},
            {"code": "AWAY", "display_name": "客胜", "odds": odds[2], "status": "OPEN"},
        ],
        "source_timestamp": now_iso(), "source_url": "https://tc.example/1",
    }
    upsert_normalized_quote(n, raw_snapshot_id=None)
    return mid


def hg_match(gid: str = "900001", home: str = "Manchester United",
             away: str = "Chelsea", odds: tuple = ("1.90", "3.40", "4.00"),
             kickoff: str = None, running: bool = False,
             updated_at: str = None, ts_delta: float = 0.0) -> dict:
    if kickoff is None:
        # Derive from the same future instant as seed_tc_match (now+2d UTC),
        # expressed as site-local +08 wall time to avoid midnight-boundary mismatch.
        base = datetime.now(timezone.utc) + timedelta(days=2)
        kt = base + timedelta(hours=8)
        h12 = kt.hour % 12 or 12
        kickoff = f"{kt.month:02d}-{kt.day:02d} {h12}:{kt.minute:02d}{'p' if kt.hour >= 12 else 'a'}"
    if updated_at is None:
        updated_at = (datetime.now(timezone.utc) - timedelta(seconds=ts_delta)).isoformat()
    return {
        "match_id": gid, "competition": "EPL", "home": home, "away": away,
        "kickoff_time": kickoff, "score_h": "", "score_c": "", "running": running,
        "quotes": {"HOME": {"odds": odds[0]}, "DRAW": {"odds": odds[1]}, "AWAY": {"odds": odds[2]}},
        "updated_at": updated_at, "source": "HG",
    }


def quote_versions(mid: str) -> dict:
    from backend.db import fetchall
    rows = fetchall(
        """SELECT s.code, q.quote_version, q.odds_decimal FROM quotes q
           JOIN selections s ON q.selection_id=s.id
           WHERE q.source='HG' AND q.canonical_match_id=?
           ORDER BY s.code, q.quote_version DESC""", (mid,))
    out: dict[str, list] = {}
    for r in rows:
        out.setdefault(r["code"], []).append((r["quote_version"], r["odds_decimal"]))
    return out


from backend.hg_canonical import sync_hg_to_canonical  # noqa: E402


def test_no_match_preserves_hg_only():
    print("== 无匹配：保留原始数据，不写canonical，不产生虚假机会 ==")
    mid = seed_tc_match("M_TC_NOMATCH", home="Arsenal", away="Liverpool")
    report = sync_hg_to_canonical([hg_match("900101", home="Real Madrid", away="Bayern")])
    check("unmatched=1", report["unmatched"] == 1, str(report))
    from backend.db import fetchone
    q = fetchone("SELECT COUNT(*) c FROM quotes WHERE source='HG' AND canonical_match_id=?", (mid,))
    check("no HG canonical quote written", int(q["c"]) == 0)


def test_matched_sync_and_engine():
    print("== 匹配同步：TC+HG 双源进引擎；无套利时不产生机会 ==")
    mid = seed_tc_match("M_TC_MATCH", home="Manchester United", away="Chelsea")
    report = sync_hg_to_canonical([hg_match("900201")])
    check("matched=1", report["matched"] == 1, str(report))
    v = quote_versions(mid)
    check("3 HG selections inserted", len(v) == 3)
    check("all version=1", all(x[0] == 1 for lst in v.values() for x in lst))
    # engine: best per state = TC 2.10 / HG 3.40 / HG 4.00 → sum_inv>=1 → no plan
    res = authoritative_calculate(mid)
    check("engine completed", res.get("status") == "COMPLETED", str(res.get("status")))
    check("no opportunity plan for book>=1", len(res.get("opportunities") or []) == 0,
          str(res.get("opportunities")))
    from backend.db import fetchall
    opps = fetchall("SELECT COUNT(*) c FROM opportunities WHERE canonical_match_id=?", (mid,))
    check("no VALID opportunity rows", int(opps[0]["c"]) == 0)


def test_arbitrage_appears_when_book_lt_1():
    print("== 赔率变化后 book<1 → 产生真实跨源机会 ==")
    mid = seed_tc_match("M_TC_ARB", home="Fulham", away="Everton")
    sync_hg_to_canonical([hg_match("900301", home="Fulham", away="Everton",
                                   odds=("1.90", "3.40", "6.00"))])
    res = authoritative_calculate(mid)
    check("engine has opportunity plan", len(res.get("opportunities") or []) == 1,
          str(res.get("opportunities")))
    from backend.db import fetchall
    opps = fetchall("SELECT COUNT(*) c FROM opportunities WHERE canonical_match_id=? AND status='VALID'", (mid,))
    check("VALID opportunity row exists", int(opps[0]["c"]) >= 1)
    # 追溯：机会引用两个来源
    from backend.db import fetchall as fa
    snap = fa("""SELECT si.source FROM calculation_snapshot_items si
                 JOIN calculation_snapshots cs ON si.snapshot_id=cs.id
                 WHERE cs.canonical_match_id=? GROUP BY si.source""", (mid,))
    sources = {r["source"] for r in snap}
    check("opportunity traceable to both TC+HG", {"TC", "HG"} <= sources, str(sources))


def test_dedup_no_new_version():
    print("== 去重：相同赔率不产生新版本 ==")
    mid = seed_tc_match("M_TC_DEDUP", home="Leeds", away="Norwich")
    sync_hg_to_canonical([hg_match("900401", home="Leeds", away="Norwich")])
    v1 = quote_versions(mid)
    sync_hg_to_canonical([hg_match("900401", home="Leeds", away="Norwich")])
    v2 = quote_versions(mid)
    check("versions unchanged", v1 == v2)
    # 变化后才 +1
    sync_hg_to_canonical([hg_match("900401", home="Leeds", away="Norwich",
                                   odds=("1.91", "3.40", "6.00"))])
    v3 = quote_versions(mid)
    check("HOME version bumped", v3["HOME"][0][0] == 2 and v3["HOME"][0][1] == "1.91", str(v3))


def test_stale_running_kickoff_rules():
    print("== 过期 / 在赛 / 开赛时间不符 均拒绝 ==")
    mid = seed_tc_match("M_TC_RULES", home="Burnley", away="Wolves")
    r1 = sync_hg_to_canonical([hg_match("900501", home="Burnley", away="Wolves", ts_delta=600)])
    check("stale skipped", r1["skipped_stale"] == 1, str(r1))
    r2 = sync_hg_to_canonical([hg_match("900502", home="Burnley", away="Wolves", running=True)])
    check("running skipped", r2["skipped_running"] == 1, str(r2))
    # kickoff 10 天前（日期不同）
    r3 = sync_hg_to_canonical([hg_match("900503", home="Burnley", away="Wolves",
                                        kickoff="01-01 08:00p")])
    check("kickoff mismatch skipped", r3["skipped_kickoff"] == 1, str(r3))
    from backend.db import fetchone
    q = fetchone("SELECT COUNT(*) c FROM quotes WHERE source='HG' AND canonical_match_id=?", (mid,))
    check("no HG quotes written", int(q["c"]) == 0)


def test_team_swap_and_alias():
    print("== 主客倒置不匹配；别名（Man Utd→Manchester United）匹配 ==")
    mid = seed_tc_match("M_TC_SWAP", home="Tottenham", away="West Ham")
    r1 = sync_hg_to_canonical([hg_match("900601", home="West Ham", away="Tottenham")])
    check("swapped teams unmatched", r1["unmatched"] == 1, str(r1))
    mid2 = seed_tc_match("M_TC_ALIAS", home="Manchester United", away="Brentford")
    r2 = sync_hg_to_canonical([hg_match("900602", home="Man Utd", away="Brentford")])
    check("alias matched", r2["matched"] == 1, str(r2))
    v = quote_versions(mid2)
    check("alias quotes written", len(v) == 3)


def test_tc_empty_scenario():
    print("== TC 无赛事：HG 全部保留为未匹配，不崩溃 ==")
    report = sync_hg_to_canonical([hg_match("900701", home="Inter", away="Milan"),
                                   hg_match("900702", home="Juventus", away="Roma")])
    check("all unmatched", report["unmatched"] == 2, str(report))
    check("no inserted", report["inserted_quotes"] == 0)


def test_latency_budget():
    print("== 同步延迟预算：单次 canonical sync < 1s ==")
    import time as _t
    mid = seed_tc_match("M_TC_LAT", home="Southampton", away="Brighton")
    t0 = _t.time()
    sync_hg_to_canonical([hg_match("900801", home="Southampton", away="Brighton")])
    dt = (_t.time() - t0) * 1000
    check(f"sync took {dt:.0f}ms < 1000ms", dt < 1000)


if __name__ == "__main__":
    test_no_match_preserves_hg_only()
    test_matched_sync_and_engine()
    test_arbitrage_appears_when_book_lt_1()
    test_dedup_no_new_version()
    test_stale_running_kickoff_rules()
    test_team_swap_and_alias()
    test_tc_empty_scenario()
    test_latency_budget()
    print(f"\n=== PASS={PASS} FAIL={FAIL} ===")
    sys.exit(1 if FAIL else 0)
