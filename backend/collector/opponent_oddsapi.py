# -*- coding: utf-8 -*-
"""The Odds API 采集器（公开 JSON API，注册免费 key 即可用）。

口径（P0.8 修正：按公司入库，消灭跨公司假套利）：
- 免费层 500 请求/月；soccer 综合（全部足球联赛）一次返回当天所有比赛的 h2h（1X2）
- 每家博彩公司各自生成一组三态报价（source = ODDSAPI:{公司}），
  只有同一家公司 h2h 三态齐全才产出（该组合物理可执行）
- 不再做跨公司 best price 合并：跨公司"最高价拼盘"数学上 Σ<1 但任何一家都无法同时成交，
  属于假套利（引擎另有 TC 腿约束兜底）
- 依赖代理（v2rayN 10808）出境；无 key 时自动 disabled，不影响系统

用法（sources.json）：
    {"name": "ODDSAPI", "enabled": true, "mode": "oddsapi",
     "proxy": "http://127.0.0.1:10808", "api_key_env": "ODDS_API_KEY",
     "interval_s": 60, "regions": "eu", "markets": "h2h"}
"""
from __future__ import annotations

import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.the-odds-api.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _company_key(title: str) -> str:
    """公司名 → 稳定标识（字母数字下划线，≤24 字符）。"""
    k = re.sub(r"[^A-Za-z0-9]", "_", title or "").strip("_")
    return k[:24] or "unknown"


def _fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    # 走配置代理（Odds API 在境外）
    proxy = os.environ.get("ODDS_PROXY", "http://127.0.0.1:10808")
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as r:
        return r.read()


def _resolve_key(source_cfg: dict[str, Any]) -> str:
    """key 优先级：环境变量 → 包外 secrets/oddsapi_key.txt → 包内 config/（警告降级）。

    包外 secrets 位于工程根同级（C:\\1\\football_hedge_p0\\secrets），不随交付包分发。
    """
    key = os.environ.get(source_cfg.get("api_key_env", "ODDS_API_KEY"), "")
    if key:
        return key.strip()
    for p, warn in (
        (Path(__file__).resolve().parents[2].parent / "secrets" / "oddsapi_key.txt", False),
        (Path(__file__).resolve().parents[2] / "config" / "oddsapi_key.txt", True),
    ):
        try:
            k = p.read_text(encoding="utf-8").strip()
            if k:
                if warn:
                    print("[warn] key 位于包内 config/，请移出到包外 secrets/（工程根同级）", file=sys.stderr)
                return k
        except Exception:
            continue
    return ""


def fetch_oddsapi(source_cfg: dict[str, Any]) -> dict[str, Any]:
    """主入口：拉取 soccer h2h → 标准化 items。"""
    import json

    key = _resolve_key(source_cfg)
    if not key:
        return {"status": "disabled", "reason": "no api key (set ODDS_API_KEY 或 config/oddsapi_key.txt)",
                "items": []}
    regions = source_cfg.get("regions", "eu")
    markets = source_cfg.get("markets", "h2h")
    sport = source_cfg.get("sport", "soccer")
    url = (f"{API_BASE}/v4/sports/{sport}/odds/?apiKey={key}"
           f"&regions={regions}&markets={markets}&oddsFormat=decimal&dateFormat=iso")
    try:
        raw = _fetch(url, timeout=25)
    except Exception as e:
        return {"status": "error", "reason": str(e)[:160], "items": []}
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        return {"status": "error", "reason": f"json: {e}", "items": []}
    if not isinstance(data, list):
        return {"status": "error", "reason": f"unexpected payload: {str(data)[:120]}",
                "items": []}

    items = []
    for g in data:
        home = (g.get("home_team") or "").strip()
        away = (g.get("away_team") or "").strip()
        commence = g.get("commence_time") or ""
        if not home or not away or not commence:
            continue
        for bm in (g.get("bookmakers") or []):
            company = (bm.get("title") or "").strip()
            if not company:
                continue
            prices = {}
            for mk in (bm.get("markets") or []):
                if mk.get("key") != markets:
                    continue
                for oc in (mk.get("outcomes") or []):
                    nm = (oc.get("name") or "").strip()
                    pr = oc.get("price")
                    if nm and isinstance(pr, (int, float)):
                        if nm not in prices or float(pr) > prices[nm]:
                            prices[nm] = float(pr)
            # 映射 outcome → HOME/DRAW/AWAY（按队伍名与标准词）
            h = prices.get(home)
            a = prices.get(away)
            d = prices.get("Draw") or prices.get("draw") or prices.get("X")
            # 同一家公司三态齐全才产出（该组合物理可执行）；缺态公司跳过
            if not (h and d and a):
                continue
            items.append({
                "source": f"ODDSAPI:{_company_key(company)}",
                "source_match_id": str(g.get("id") or ""),
                "competition": (g.get("sport_title") or "football"),
                "home_team": home,
                "away_team": away,
                "kickoff_time_utc": commence.replace("Z", "+00:00"),
                "market_type": "MATCH_RESULT",
                "line": None,
                "selections": [
                    {"code": "HOME", "display_name": "主胜", "odds": str(h), "status": "OPEN"},
                    {"code": "DRAW", "display_name": "平局", "odds": str(d), "status": "OPEN"},
                    {"code": "AWAY", "display_name": "客胜", "odds": str(a), "status": "OPEN"},
                ],
                "source_timestamp": _now_iso(),
                "source_url": url.split("?")[0],
            })
    return {"status": "ok" if items else "no_matches",
            "matches": len(items), "items": items}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
