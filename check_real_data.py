"""核对数据库内的真实体彩数据（只统计 source=TC）。"""
from __future__ import annotations

import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.db import fetchall, fetchone  # noqa: E402

print("=" * 64)
total_matches = fetchone("SELECT COUNT(*) c FROM matches")["c"]
tc_matches = fetchall(
    """SELECT DISTINCT m.id, m.competition, m.home_team, m.away_team,
                      m.kickoff_time_utc, m.status, m.updated_at
       FROM matches m ORDER BY m.kickoff_time_utc""")
tc_quotes = fetchone("SELECT COUNT(*) c FROM quotes WHERE source='TC'")["c"]
print(f"比赛总数: {total_matches} | TC 赔率条数: {tc_quotes}")
print("=" * 64)

for m in tc_matches:
    print(f"\n{m['home_team']}  VS  {m['away_team']}")
    print(f"  赛事: {m['competition']} | 开赛(UTC): {m['kickoff_time_utc']} | 状态: {m['status']}")
    q = fetchall(
        """SELECT s.code, q.odds_decimal, q.quote_version, q.source_status,
                  q.is_valid, q.received_at, q.source_url
           FROM quotes q JOIN selections s ON q.selection_id=s.id
           JOIN markets mk ON q.market_id=mk.id
           WHERE mk.canonical_match_id=? AND q.source='TC'
           ORDER BY s.code, q.quote_version DESC""", (m["id"],))
    latest = {}
    for r in q:
        if r["code"] not in latest:
            latest[r["code"]] = r
    odds = {}
    for code in ("HOME", "DRAW", "AWAY"):
        if code in latest:
            r = latest[code]
            odds[code] = Decimal(r["odds_decimal"])
            print(f"  {code:4} 赔率 {r['odds_decimal']:>6} | 版本 V{r['quote_version']} "
                  f"| {r['source_status']} | valid={r['is_valid']}")
    if len(odds) == 3:
        s = sum(1 / o for o in odds.values())
        rr = 1 / s
        print(f"  TC 返奖率: {rr*100:.2f}%  (抽水 {(s-1)*100:.2f}%)  -> 单一渠道无套利")
    if q:
        print(f"  最近入库: {q[0]['received_at']}")
        print(f"  来源: {q[0]['source_url']}")

print("\n" + "=" * 64)
obs = fetchone("SELECT COUNT(*) c, MAX(received_at) last FROM source_observations")
raw = fetchone("SELECT COUNT(*) c FROM raw_snapshots")
print(f"原始快照: {raw['c']} | 观察记录: {obs['c']} | 最新观察: {obs['last']}")
