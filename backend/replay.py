# -*- coding: utf-8 -*-
"""P0.8 #22 Replay 测试骨架：同一份真实历史快照 → 永远得到可重复结果。

回放链路：raw_snapshots(真实抓取) → 解析(与采集器同一函数) → 标准化 → 摘要 hash
计算链路：库内跨端报价 → 权威计算 ×2 → 输出 hash 恒定 = 可重复性证明。

用法：
    python -m backend.replay                  # 回放最近 TC+DAFABET 各 1 条快照
    python -m backend.replay --snapshot RAW_x  # 指定快照回放
退出码：0=可重复性通过；1=失败。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.db import db  # noqa: E402
from backend.collector.collector import (  # noqa: E402
    _parse_tc_fb_items,
    _parse_dafabet_extract,
    normalize_payload,
)
from backend.engine.engine import authoritative_calculate  # noqa: E402
from backend.data_process.match_merger import run_merge  # noqa: E402

CONFIG = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))


def _norm_hash(obj) -> str:
    """规范化 JSON → sha256（Decimal/顺序无关 → 稳定 hash）。"""
    def _clean(o):
        if isinstance(o, Decimal):
            return str(o)
        if isinstance(o, dict):
            return {k: _clean(v) for k, v in sorted(o.items())}
        if isinstance(o, (list, tuple)):
            return [_clean(v) for v in o]
        return o
    return hashlib.sha256(
        json.dumps(_clean(obj), ensure_ascii=False, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _pick_snapshot(raw_id: str | None) -> list[sqlite3.Row]:
    with db() as conn:
        if raw_id:
            return conn.execute(
                "SELECT * FROM raw_snapshots WHERE id=? ORDER BY received_at DESC LIMIT 1",
                (raw_id,)).fetchall()
        # 最近 TC + DAFABET 各 1 条
        out = []
        for src in ("TC", "DAFABET"):
            out += conn.execute(
                "SELECT * FROM raw_snapshots WHERE source=? ORDER BY received_at DESC LIMIT 1",
                (src,)).fetchall()
        return out


def replay_parsing(raw_id: str | None = None) -> dict:
    """解析可重复性：快照 → items（复用采集器解析函数）→ items_hash。"""
    cfg_by_name = {c["name"]: c for c in CONFIG["sources"]}
    rows = _pick_snapshot(raw_id)
    results = []
    for r in rows:
        path = Path(r["storage_path"])
        raw_text = path.read_text(encoding="utf-8")
        raw = json.loads(raw_text)
        cfg = cfg_by_name.get(r["source"])
        if r["source"] == "TC":
            items = _parse_tc_fb_items(raw, {}, cfg)
        elif r["source"] == "DAFABET":
            items = _parse_dafabet_extract(raw, cfg)
        else:
            results.append({"source": r["source"], "status": "SKIP"})
            continue
        normed = [normalize_payload(r["source"], it) for it in items]
        # source_timestamp 是解析时刻的运行时工件，非快照内容 —— 剔除后 hash 才代表
        # "同一快照 → 同一标准化报价" 的可重复性
        for n in normed:
            n.pop("source_timestamp", None)
        results.append({
            "source": r["source"], "raw_id": r["id"],
            "matches": len(items),
            "items_hash": _norm_hash(normed),
        })
    return {"parsing": results}


def replay_calculation() -> dict:
    """计算可重复性：同一跨端比赛权威计算 ×2 → 结果 hash 一致。"""
    with db() as conn:
        m = conn.execute("""SELECT m.id FROM matches m
            JOIN markets mk ON mk.canonical_match_id=m.id
            JOIN quotes q ON q.market_id=mk.id
            WHERE q.source='DAFABET' AND EXISTS (
                SELECT 1 FROM markets mk2 JOIN quotes q2 ON q2.market_id=mk2.id
                WHERE mk2.canonical_match_id=m.id AND q2.source='TC')
            GROUP BY m.id LIMIT 1""").fetchone()
    if not m:
        return {"calculation": {"status": "NO_CROSS_END_MATCH"}}
    mid = m["id"]
    r1 = authoritative_calculate(mid)
    r2 = authoritative_calculate(mid)
    h1 = _norm_hash({k: r1.get(k) for k in ("status", "classification", "min_pnl", "max_pnl",
                                            "total_stake", "coverage_status", "infeasible_markets")})
    h2 = _norm_hash({k: r2.get(k) for k in ("status", "classification", "min_pnl", "max_pnl",
                                            "total_stake", "coverage_status", "infeasible_markets")})
    return {"calculation": {
        "match_id": mid,
        "run1_hash": h1, "run2_hash": h2,
        "repeatable": h1 == h2,
        "status": r1.get("status"),
    }}


def main() -> int:
    raw_id = None
    if len(sys.argv) > 2 and sys.argv[1] == "--snapshot":
        raw_id = sys.argv[2]
    print("=" * 60)
    print("P0.8 Replay：真实快照 → 可重复结果")
    print("=" * 60)
    p = replay_parsing(raw_id)
    for r in p["parsing"]:
        print(f"  [{r['source']}] {r.get('raw_id','?')[:40]} 解析 {r.get('matches',0)} 场  hash={r.get('items_hash','SKIP')}")
    c = replay_calculation()["calculation"]
    print(f"  计算复算 ×2: {c.get('status')}  hash {c.get('run1_hash')} == {c.get('run2_hash')} → {'可重复 ✓' if c.get('repeatable') else '不可重复 ✗'}")
    ok = all(r.get("items_hash") for r in p["parsing"]) and c.get("repeatable") is True
    print(f"汇总：Replay {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
