"""端到端冒烟测试（P0 模块#15）：采集 → 计算 → 入库 → 验证。"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db import db, fetchall, init_db
from backend.collector.collector import collect
from backend.engine.engine import authoritative_calculate, refresh_match_status


def test_e2e_tc_collect_and_calculate():
    """TC 采集 → 计算 → opportunities 入库。"""
    init_db()
    cfg = {
        "name": "TC", "enabled": True, "mode": "tc_public_api",
        "url": "https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry?clientCode=3001",
        "timeout_seconds": 20,
        "headers": {"User-Agent": "Mozilla/5.0", "Referer": "https://www.sporttery.cn/"},
    }
    r = collect(cfg)
    assert r["status"] == "OK", f"collect failed: {r}"
    assert r["matches"] >= 1, f"no matches: {r}"

    # 只挑未开赛且仍在售的比赛跑权威计算（引擎仅对 SCHEDULED + 未来开赛计算）
    now_iso = datetime.now(timezone.utc).isoformat()
    m = fetchall(
        """SELECT id FROM matches WHERE status='SCHEDULED' AND kickoff_time_utc > ?
           ORDER BY kickoff_time_utc LIMIT 1""", (now_iso,))
    if not m:
        print("[E2E] SKIP: 当前无未开赛在售赛事（环境相关，非回归）")
        return
    result = authoritative_calculate(m[0]["id"])
    assert result["status"] == "COMPLETED", f"calc: {result.get('status')}"

    # 比赛状态已刷新
    status = refresh_match_status(m[0]["id"])
    assert status in ("SCHEDULED", "PRE_MATCH", "LIVE", "FINISHED")

    # opportunities 表有 grade 字段
    opp = fetchall("SELECT classification, grade FROM opportunities LIMIT 1")
    if opp:
        assert opp[0]["grade"] in ("A", "B", "C", "D")

    print(f"[E2E] OK: {r['matches']} matches, status={status}")


if __name__ == "__main__":
    test_e2e_tc_collect_and_calculate()
    print("=== E2E PASSED ===")
