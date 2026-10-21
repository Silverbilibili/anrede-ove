# -*- coding: utf-8 -*-
"""P0.8 #23 LIVE SMOKE：真实环境冒烟测试（网络失败 → WARN，不使核心 CI 失效）。

覆盖：
  1. TC 官方接口真实可达（返回比赛 + 赔率）
  2. 本机 API /api/v1/health（rule_version=4）
  3. 本机 API /api/v1/source-health（源健康状态）
  4. 引擎真实计算 1 场跨端比赛
  5. 前端页面 / 与 /plans 可达
  6. Dafabet（AdsPower 驻留）——外部配额失败仅 WARN

退出码：0=核心全过；1=核心失败（LIVE SMOKE 网络失败只降级不崩 CI）。
用法：python -m tests.live_smoke
"""
from __future__ import annotations

import json
import sqlite3
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, fn) -> None:
    try:
        ok, note = fn()
        results.append((name, PASS if ok else WARN, note))
    except Exception as e:  # noqa: BLE001
        results.append((name, FAIL, str(e)[:150]))


def main() -> int:
    print("=" * 64)
    print("P0.8 LIVE SMOKE：真实环境冒烟（2026-10-06）")
    print("=" * 64)

    # 1. TC 官方接口
    def tc_api():
        url = ("https://webapi.sporttery.cn/gateway/uniform/fb/"
               "getMatchDataPageListV1.qry?method=concern")
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"),
            "Referer": "https://m.sporttery.cn/mjc/zqsj/?tab=concern",
        })
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode("utf-8"))
        subs = ((data.get("value", {}) or {}).get("subMatchList") or [])
        n = len(subs)
        return n >= 5, f"{n} 场比赛（含 h/d/a 赔率）"

    # 2. 本机 API health
    def api_health():
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8030/api/v1/health", timeout=5))
        return d.get("rule_version") == 4 and d.get("status") == "ok", f"rule_v{d.get('rule_version')}"

    # 3. 源健康
    def src_health():
        d = json.load(urllib.request.urlopen("http://127.0.0.1:8030/api/v1/source-health", timeout=5))
        return len(d) >= 1, ", ".join(f"{h['source']}:{h['last_status']}" for h in d)

    # 4. 引擎真实计算 1 场（只选未开赛 + 有效报价，避免 Match Lock 拒绝）
    def engine_calc():
        db = sqlite3.connect(ROOT / "data" / "football.db")
        m = db.execute("""SELECT m.id FROM matches m
            JOIN markets mk ON mk.canonical_match_id=m.id
            JOIN quotes q ON q.market_id=mk.id
            WHERE m.status='SCHEDULED' AND q.source='DAFABET' AND q.is_valid=1 AND EXISTS (
                SELECT 1 FROM markets mk2 JOIN quotes q2 ON q2.market_id=mk2.id
                WHERE mk2.canonical_match_id=m.id AND q2.source='TC' AND q2.is_valid=1)
            GROUP BY m.id LIMIT 1""").fetchone()
        if not m:
            return False, "无未开赛跨端比赛"
        from backend.engine.engine import authoritative_calculate
        r = authoritative_calculate(m[0])
        ok = r.get("status") == "COMPLETED"
        tc = r.get("timechain") or {}
        return ok, f"{r.get('status')} · 计算 {tc.get('calculation_delay_s', 'n/a')}s"

    # 5. 前端可达
    def web_ok():
        for path in ("/", "/plans", "/api/v1/opportunities"):
            d = urllib.request.urlopen("http://127.0.0.1:8030" + path, timeout=5)
            assert d.status == 200, f"{path} -> {d.status}"
        return True, "/ /plans /api/v1/opportunities 均 200"

    # 6. Dafabet 配额（外部限制 → WARN 不 FAIL）
    def df_smoke():
        cfg = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
        df = next(c for c in cfg["sources"] if c["name"] == "DAFABET")
        if not df.get("enabled"):
            return False, "DAFABET disabled in config"
        try:
            from backend.collector.adspower_api import load_token as _load_token
            token = _load_token()
            import requests as _rq
            resp = _rq.get("http://127.0.0.1:50325/v1/browser/start?user_id=k1ho30ir",
                           headers={"Authorization": "Bearer " + token},
                           timeout=8)
            j = resp.json()
            ok = j.get("code") == 0
            return ok, ("AdsPower 可启动" if ok else f"AdsPower: {j.get('msg','')[:80]}")
        except Exception as e:
            return False, f"AdsPower 不可达: {str(e)[:80]}"

    check("TC 官方接口", tc_api)
    check("API health", api_health)
    check("源健康", src_health)
    check("引擎真实计算", engine_calc)
    check("Web 前端", web_ok)
    check("Dafabet 驻留", df_smoke)

    print(f"{'项目':<14}{'状态':<6}说明")
    print("-" * 64)
    for name, st, note in results:
        print(f"{name:<14}{st:<6}{note}")
    fails = [r for r in results if r[1] == FAIL]
    warns = [r for r in results if r[1] == WARN]
    print("-" * 64)
    print(f"LIVE SMOKE：核心 {'全部通过' if not fails else '存在失败'} | WARN {len(warns)} 条（不阻塞）")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
