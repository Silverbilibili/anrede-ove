"""P0 一键闭环 pipeline：采集（TC+Dafabet）→ 归并 → 全市场权威计算 → 机会摘要。

用法：
    python -m backend.pipeline            # 全跑
    python -m backend.pipeline --no-collect   # 跳过采集只算已有数据
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.collector.collector import collect  # noqa: E402
from backend.data_process.match_merger import run_merge  # noqa: E402
from backend.engine.engine import authoritative_calculate  # noqa: E402

DB = ROOT / "data" / "football.db"


def load_sources() -> list[dict]:
    cfg = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
    return cfg["sources"]


def collect_all(sources: list[dict]) -> dict:
    report = {}
    for src in sources:
        if not src.get("enabled"):
            continue
        t0 = time.time()
        try:
            r = collect(src)
            report[src["name"]] = {**r, "elapsed_s": round(time.time() - t0, 1)}
        except Exception as e:
            report[src["name"]] = {"status": "ERROR", "error": str(e)[:200],
                                   "elapsed_s": round(time.time() - t0, 1)}
    return report


def calculate_all(verbose: bool = True) -> list[dict]:
    """对所有（两端都有报价的）比赛做权威计算，输出机会摘要。"""
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT m.id, m.home_team, m.away_team FROM matches m
        JOIN markets mk ON mk.canonical_match_id=m.id
        JOIN quotes q ON q.market_id=mk.id
        WHERE q.source='DAFABET' AND EXISTS (
            SELECT 1 FROM markets mk2 JOIN quotes q2 ON q2.market_id=mk2.id
            WHERE mk2.canonical_match_id=m.id AND q2.source='TC')
        GROUP BY m.id""").fetchall()
    summary = []
    for r in rows:
        try:
            res = authoritative_calculate(r["id"])
        except Exception as e:
            summary.append({"match": f"{r['home_team']} vs {r['away_team']}",
                            "status": "ERROR", "error": str(e)[:120],
                            "opportunities": [], "infeasible": []})
            continue
        opps = res.get("opportunities") or []
        inf = res.get("infeasible_markets") or []
        item = {
            "match": f"{r['home_team']} vs {r['away_team']}",
            "match_id": r["id"],
            "opportunities": [],
            "infeasible": [{"market_id": i.get("market_id", ""), "reason": i.get("reason")} for i in inf],
        }
        for o in opps:
            item["opportunities"].append({
                "classification": o.get("classification"),
                "min_net_pnl": o.get("min_net_pnl"),
                "total_stake": o.get("total_stake"),
                "roi": o.get("roi"),
                "coverage": o.get("coverage_status"),
                "stakes": o.get("stakes"),
            })
        summary.append(item)
        if verbose:
            tag = f"机会 {len(item['opportunities'])} 条"
            if item["opportunities"]:
                tag += " | " + "; ".join(
                    f"{o['classification']} pmin={o['min_net_pnl']} stake={o['total_stake']} roi={o['roi']}%"
                    for o in item["opportunities"][:3])
            print(f"  {item['match']}: {tag}")
            for i in item["infeasible"][:1]:
                print(f"    infeasible: {i['reason']}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-collect", action="store_true", help="跳过采集")
    ap.add_argument("--no-merge", action="store_true", help="跳过归并")
    args = ap.parse_args()

    # P0.8 工程债 #3：启动自动建表（新环境不再 "no such table: matches"）
    from backend.db import init_db, seed_capabilities
    init_db()
    seed_capabilities()
    print("=== 0. 数据库就绪（init_db + 能力矩阵） ===")

    sources = load_sources()
    if not args.no_collect:
        print("=== 1. 采集 ===")
        report = collect_all(sources)
        for name, r in report.items():
            print(f"  {name}: status={r['status']} matches={r.get('matches')} "
                  f"quotes_changed={r.get('quotes_changed')} {r.get('elapsed_s')}s"
                  + (f" error={r.get('error')}" if r.get("error") else ""))
    else:
        print("=== 1. 跳过采集 ===")

    if not args.no_merge:
        print("=== 2. 归并 DAFABET → TC ===")
        r = run_merge(verbose=False)
        print(f"  归并报价 {r['merged_quotes']} 条")
        for s in r["skipped"]:
            print(f"  跳过: {s}")
    else:
        print("=== 2. 跳过归并 ===")

    print("=== 3. 跨端权威计算 ===")
    summary = calculate_all()
    print(f"  共处理 {len(summary)} 场比赛")
    opp_total = sum(len(s["opportunities"]) for s in summary)
    print(f"  ★ 当前套利机会：{opp_total} 条")

    (ROOT / "data" / "pipeline_report.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print("  报告已写 data/pipeline_report.json")


if __name__ == "__main__":
    main()
