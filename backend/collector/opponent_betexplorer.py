# -*- coding: utf-8 -*-
"""BetExplorer 参考源采集器（免费、无登录、走代理）。

价值：未来比赛列表（队名 slug + 详情链接 + 页面时间），辅助跨源匹配与赛程参考。
局限：列表页不显示赔率；详情页盘口出现滞后（博彩公司临近开赛才出盘）——
      因此本模块不产赔率，只产赛程信息（P0 参考源，enabled 默认 false）。

用法（sources.json）：
    {"name": "BETEXPLORER", "enabled": false, "mode": "betexplorer",
     "proxy": "http://127.0.0.1:10808", "interval_s": 600}
"""
from __future__ import annotations

import datetime
import http.cookiejar
import re
import urllib.request
from typing import Any

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
BASE = "https://www.betexplorer.com"


def fetch_betexplorer(source_cfg: dict[str, Any]) -> dict[str, Any]:
    proxy = source_cfg.get("proxy", "http://127.0.0.1:10808")
    cj = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cj),
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}))

    def _get(url: str, timeout: int = 14) -> bytes:
        req = urllib.request.Request(url, headers={
            "User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        with opener.open(req, timeout=timeout) as r:
            return r.read()

    try:
        _get(f"{BASE}/")  # 建立 cookie（年龄确认门）
        today = datetime.date.today()
        items = []
        for delta in (0, 1):
            d = today + datetime.timedelta(days=delta)
            ds = d.strftime("%Y%m%d")
            try:
                html = _get(f"{BASE}/soccer/?date={ds}").decode("utf-8", "ignore")
            except Exception:
                continue
            for row in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
                hrefs = re.findall(r'href="(/football/[^"]+)"', row)
                detail = next((x for x in hrefs if re.search(r"/\w{8}/$", x)), None)
                if not detail:
                    continue
                tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
                cells = [re.sub(r"<[^>]+>", "", t).strip().replace("\u00a0", "")
                         for t in tds]
                # 第 1 格通常为 "HH:MM主队 - 客队"（BetExplorer 页面时区）
                first = cells[0] if cells else ""
                mt = re.match(r"(\d{2}:\d{2})(.*)", first)
                page_t = mt.group(1) if mt else ""
                teams = (mt.group(2) if mt else first).split(" - ")
                if len(teams) != 2:
                    continue
                items.append({
                    "source_match_id": detail,
                    "competition": detail.split("/")[3] if len(detail.split("/")) > 3 else "football",
                    "home_team": teams[0].strip(),
                    "away_team": teams[1].strip(),
                    # 页面时间按 Europe/Amsterdam(CET/CEST) 近似转 UTC（参考值）
                    "kickoff_time_utc": _page_time_to_utc(d, page_t),
                    "market_type": "MATCH_RESULT",
                    "line": None,
                    "selections": [],
                    "source_timestamp": _now_iso(),
                    "source_url": f"{BASE}/soccer/?date={ds}",
                    "detail_url": f"{BASE}{detail}",
                    "odds_available": False,
                })
        return {"status": "ok" if items else "no_matches",
                "matches": len(items), "items": items,
                "date": today.strftime("%Y%m%d")}
    except Exception as e:
        return {"status": "error", "reason": str(e)[:160], "items": []}


def _page_time_to_utc(d: datetime.date, t: str) -> str:
    """BetExplorer 列表时间按欧洲中部时区（CET/CEST）近似转 UTC。"""
    import zoneinfo
    try:
        tz = zoneinfo.ZoneInfo("Europe/Amsterdam")
        dt = datetime.datetime.combine(d, datetime.time(*map(int, t.split(":"))))
        return dt.replace(tzinfo=tz).astimezone(datetime.timezone.utc).isoformat()
    except Exception:
        return f"{d}T{t}:00+00:00"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
