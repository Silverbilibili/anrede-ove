from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dateutil import parser as dtparser

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

from backend.db import db, row_json

ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = ROOT / "data" / "raw"
COLLECTOR_VERSION = "p0.1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_team(name: str) -> str:
    s = (name or "").strip().lower()
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def match_key(home: str, away: str, kickoff_utc: str) -> str:
    rounded = dtparser.isoparse(kickoff_utc).astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()
    raw = f"football|{canonical_team(home)}|{canonical_team(away)}|{rounded}"
    return "M_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def source_market_key(match_id: str, market_type: str, line: Any) -> str:
    raw = f"{match_id}|{market_type}|{line}"
    return "MK_" + hashlib.sha1(raw.encode()).hexdigest()[:16]


def safe_decimal(value: Any) -> Decimal | None:
    try:
        d = Decimal(str(value))
        return d if d > 1 else None
    except (InvalidOperation, TypeError):
        return None


def implied_probability(odds: Decimal) -> Decimal:
    return Decimal("1") / odds


def de_vig(odds_values: list[Decimal]) -> list[Decimal]:
    probs = [implied_probability(x) for x in odds_values]
    total = sum(probs, Decimal("0"))
    if total <= 0:
        return []
    return [p / total for p in probs]


def normalize_payload(source: str, item: dict[str, Any]) -> dict[str, Any]:
    kickoff = dtparser.isoparse(item["kickoff_time_utc"]).astimezone(timezone.utc).isoformat()
    home = str(item["home_team"]).strip()
    away = str(item["away_team"]).strip()
    match_id = match_key(home, away, kickoff)
    market_type = str(item["market_type"]).upper()
    line = item.get("line")
    market_id = source_market_key(match_id, market_type, line)

    normalized_selections = []
    odds = []
    for sel in item.get("selections", []):
        d = safe_decimal(sel.get("odds"))
        status = str(sel.get("status", "OPEN")).upper()
        code = str(sel["code"]).upper()
        if d is not None:
            odds.append(d)
        normalized_selections.append({
            "code": code,
            "display_name": sel.get("display_name") or code,
            "odds": str(d) if d is not None else None,
            "status": status,
        })

    probabilities = de_vig(odds)
    prob_iter = iter(probabilities)
    for sel in normalized_selections:
        if sel["odds"] is not None:
            sel["implied_probability"] = str(implied_probability(Decimal(sel["odds"])))
            sel["de_vig_probability"] = str(next(prob_iter))
        else:
            sel["implied_probability"] = None
            sel["de_vig_probability"] = None

    return {
        "source": source,
        "source_match_id": item.get("source_match_id") or match_id,
        "competition": item.get("competition"),
        "home_team": home,
        "away_team": away,
        "kickoff_time_utc": kickoff,
        "match_id": match_id,
        "market_id": market_id,
        "market_type": market_type,
        "line": line,
        "selections": normalized_selections,
        "source_timestamp": item.get("source_timestamp") or now_iso(),
        "source_url": item.get("source_url"),
        "basic_metrics": {
            "selection_count": len(odds),
            "book_sum_implied_probability": str(sum((implied_probability(x) for x in odds), Decimal("0"))) if odds else None,
            "overround": str(sum((implied_probability(x) for x in odds), Decimal("0")) - Decimal("1")) if odds else None,
        },
    }


def save_raw(source: str, raw: Any, source_url: str | None) -> str:
    received = datetime.now(timezone.utc)
    stamp = received.strftime("%Y%m%dT%H%M%S%fZ")
    raw_text = json.dumps(raw, ensure_ascii=False, default=str, separators=(",", ":")) if not isinstance(raw, str) else raw
    digest = hashlib.sha256(raw_text.encode()).hexdigest()
    sid = f"RAW_{source}_{stamp}_{digest[:10]}"
    folder = RAW_DIR / received.strftime("%Y/%m/%d") / source
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.json"
    path.write_text(raw_text, encoding="utf-8")
    with db() as conn:
        conn.execute(
            "INSERT INTO raw_snapshots(id,source,captured_at,received_at,collector_version,content_type,payload_hash,storage_path,source_url,metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sid, source, now_iso(), now_iso(), COLLECTOR_VERSION, "application/json", digest, str(path), source_url, row_json({"bytes": len(raw_text.encode('utf-8'))})),
        )
    return sid


def upsert_normalized_quote(n: dict[str, Any], raw_snapshot_id: str) -> list[str]:
    ts = now_iso()
    ids = []
    with db() as conn:
        conn.execute(
            "INSERT INTO matches(id,sport,competition,home_team,away_team,kickoff_time_utc,status,canonical_version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET competition=excluded.competition, updated_at=excluded.updated_at",
            (n["match_id"], "football", n.get("competition"), n["home_team"], n["away_team"], n["kickoff_time_utc"], "SCHEDULED", 1, ts, ts),
        )
        conn.execute(
            "INSERT INTO source_match_mappings(source,source_match_id,canonical_match_id,match_confidence,mapping_status,source_home_name,source_away_name,source_kickoff_time,first_seen_at,last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source,source_match_id) DO UPDATE SET canonical_match_id=excluded.canonical_match_id,match_confidence=excluded.match_confidence,mapping_status=excluded.mapping_status,last_seen_at=excluded.last_seen_at",
            (n["source"], n["source_match_id"], n["match_id"], 0.999, "AUTO_CONFIRMED", n["home_team"], n["away_team"], n["kickoff_time_utc"], ts, ts),
        )
        conn.execute(
            "INSERT INTO markets(id,canonical_match_id,market_type,line,status,settlement_rule_id,version) VALUES (?,?,?,?,?,?,1) ON CONFLICT(id) DO NOTHING",
            (n["market_id"], n["match_id"], n["market_type"], str(n.get("line")) if n.get("line") is not None else None, "OPEN", n["market_type"],),
        )
        for sel in n["selections"]:
            selection_id = "SEL_" + hashlib.sha1(f"{n['market_id']}|{sel['code']}".encode()).hexdigest()[:16]
            conn.execute(
                "INSERT INTO selections(id,market_id,code,display_name,settlement_outcome_code) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name",
                (selection_id, n["market_id"], sel["code"], sel["display_name"], sel["code"]),
            )
            if sel["odds"] is None:
                continue
            # P0.8 版本语义修正：payload_hash 只基于赔率内容（去掉 source_timestamp），
            # 赔率没变 → hash 相同 → 不创建新 quote version（观察仍全量留存）
            quote_payload = f"{n['source']}|{n['match_id']}|{n['market_id']}|{sel['code']}|{sel['odds']}"
            payload_hash = hashlib.sha256(quote_payload.encode()).hexdigest()
            latest = conn.execute(
                "SELECT q.quote_version, q.odds_decimal, q.payload_hash FROM quotes q WHERE q.source=? AND q.market_id=? AND q.selection_id=? ORDER BY q.quote_version DESC LIMIT 1",
                (n["source"], n["market_id"], selection_id),
            ).fetchone()
            new_version = (latest[0] + 1) if latest else 1
            # P0.8 证据层：无论赔率是否变化，本次抓取都必须留存 observation
            obs_id = f"OBS_{uuid.uuid4().hex[:16]}"
            changed_quote_id = None
            changed_quote_version = None
            if not (latest and latest[1] == sel["odds"] and latest[2] == payload_hash):
                qid = f"Q_{n['source']}_{uuid.uuid4().hex[:16]}"
                conn.execute(
                    "INSERT INTO quotes(id,source,source_quote_id,canonical_match_id,market_id,selection_id,odds_decimal,source_status,source_timestamp,received_at,normalized_at,quote_version,payload_hash,source_url,raw_snapshot_id,is_valid,invalid_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (qid, n["source"], None, n["match_id"], n["market_id"], selection_id, sel["odds"], sel["status"], n["source_timestamp"], ts, ts, new_version, payload_hash, n.get("source_url"), raw_snapshot_id, 1 if sel["status"] == "OPEN" else 0, None if sel["status"] == "OPEN" else "SOURCE_NOT_OPEN"),
                )
                changed_quote_id, changed_quote_version = qid, new_version
                # 记录报价变化事件（V1 模块#5）
                event_type = "INITIAL" if new_version == 1 else "CHANGED"
                old_odds = (latest[1] if latest else None)
                conn.execute(
                    """INSERT INTO quote_events
                       (id,source,canonical_match_id,market_id,selection_id,
                        event_type,old_odds,new_odds,quote_version,received_at,created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"QEV_{uuid.uuid4().hex[:16]}", n["source"], n["match_id"],
                     n["market_id"], selection_id, event_type, old_odds,
                     str(sel["odds"]), new_version, ts, ts),
                )
                ids.append(qid)
            conn.execute(
                """INSERT INTO source_observations
                   (id,source,source_match_id,source_market_id,source_selection_id,
                    odds,line,market_type,source_status,source_timestamp,
                    collected_at,received_at,raw_snapshot_id,payload_hash,collector_version,
                    quote_id,quote_version,canonical_match_id,market_id,selection_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (obs_id, n["source"], n.get("source_match_id"), n.get("source_market_id"),
                 n.get("source_selection_id"), sel["odds"], str(n.get("line")) if n.get("line") is not None else None,
                 n["market_type"], sel["status"], n.get("source_timestamp"),
                 ts, ts, raw_snapshot_id, payload_hash, COLLECTOR_VERSION,
                 changed_quote_id, changed_quote_version, n["match_id"],
                 n["market_id"], selection_id),
            )
    return ids


def fetch_http_json(source_cfg: dict[str, Any]) -> Any:
    req = urllib.request.Request(
        source_cfg["url"],
        headers=source_cfg.get("headers", {"User-Agent": "Mozilla/5.0"}),
    )
    with urllib.request.urlopen(req, timeout=source_cfg.get("timeout_seconds", 20)) as r:
        return json.loads(r.read().decode("utf-8"))


def extract_json_path(payload: Any, path: str) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return []
    return current


def fetch_browser(source_cfg: dict[str, Any]) -> str:
    if sync_playwright is None:
        raise RuntimeError("playwright 未安装")
    cdp_url = source_cfg.get("cdp_url")
    url = source_cfg.get("url")
    if not cdp_url or not url:
        raise RuntimeError("browser_cdp 模式缺少 cdp_url 或 url")
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url)
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("CDP 浏览器没有可用 context")
        page = contexts[0].pages[0] if contexts[0].pages else contexts[0].new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=source_cfg.get("timeout_seconds", 30) * 1000)
        page.wait_for_timeout(1000)
        return page.content()


def parse_browser_html(source: str, html: str, source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """
    通用浏览器 Collector 不猜页面结构。
    需要在 config/sources.json 中补充实际 selector 后再使用。
    页面结构不同，就通过这个函数替换成具体 adapter。
    """
    selectors = source_cfg.get("selectors", {})
    if not selectors:
        raise RuntimeError(f"{source}: 未配置 selectors。请按实际页面 DOM 实现 source adapter。")
    if BeautifulSoup is None:
        raise RuntimeError("beautifulsoup4 未安装，browser_cdp 模式不可用")

    soup = BeautifulSoup(html, "html.parser")
    match_nodes = soup.select(selectors.get("match", ""))
    out = []
    for node in match_nodes:
        def txt(key: str) -> str:
            sel = selectors.get(key)
            el = node.select_one(sel) if sel else None
            return el.get_text(" ", strip=True) if el else ""
        selections = []
        for code, selector in selectors.get("selections", {}).items():
            el = node.select_one(selector)
            if el:
                selections.append({"code": code, "display_name": code, "odds": el.get_text(" ", strip=True), "status": "OPEN"})
        out.append({
            "source_match_id": node.get("data-match-id") or txt("source_match_id"),
            "competition": txt("competition"),
            "home_team": txt("home_team"),
            "away_team": txt("away_team"),
            "kickoff_time_utc": txt("kickoff_time_utc"),
            "market_type": source_cfg.get("market_type", "MATCH_RESULT"),
            "line": source_cfg.get("line"),
            "selections": selections,
            "source_timestamp": now_iso(),
            "source_url": source_cfg.get("url"),
        })
    return out


def _parse_tc_public_api(raw: Any) -> list[dict[str, Any]]:
    """把竞彩官方 API 返回转成 normalize_payload 期望的格式。

    官方返回结构:
      value.matchInfoList[].subMatchList[] = 比赛列表
      每场: matchNumStr / homeTeamAbbName / awayTeamAbbName / matchTime /
            had = {h, d, a, updateTime}  (胜平负赔率)
    """
    out = []
    groups = (raw.get("value") or {}).get("matchInfoList") or []
    for g in groups:
        biz_date = g.get("businessDate", "")
        for m in g.get("subMatchList", []):
            had = m.get("had") or {}
            if not had.get("h"):
                continue
            # 竞彩 matchTime 是 "HH:MM:SS"，businessDate 是 "YYYY-MM-DD"
            kickoff = f"{biz_date}T{m.get('matchTime','00:00:00')}+08:00"
            out.append({
                "source_match_id": str(m.get("matchId") or m.get("matchNumStr")),
                "competition": m.get("leagueAbbName") or m.get("leagueAllName"),
                "home_team": m.get("homeTeamAbbName") or m.get("homeTeamAllName"),
                "away_team": m.get("awayTeamAbbName") or m.get("awayTeamAllName"),
                "kickoff_time_utc": kickoff,
                "market_type": "MATCH_RESULT",
                "line": None,
                "selections": [
                    {"code": "HOME", "display_name": "主胜", "odds": had["h"], "status": "OPEN"},
                    {"code": "DRAW", "display_name": "平局", "odds": had["d"], "status": "OPEN"},
                    {"code": "AWAY", "display_name": "客胜", "odds": had["a"], "status": "OPEN"},
                ],
                "source_timestamp": f"{had.get('updateDate','')}T{had.get('updateTime','')}",
                "source_url": "https://www.sporttery.cn/",
            })
    return out


# ---------------------------------------------------------------------------
# TC 官方接口 · fb 路径（2026-10-06 实测：football 路径被 EdgeOne 风控，
# fb 路径不受控。链路 = 赛程列表 getMatchDataPageListV1 → 单场赔率 getMatchGeneral）
# ---------------------------------------------------------------------------

# 竞彩状态映射（zqsj.js 注释）：1=待开售 2=已开售 3=暂停销售 4=未开赛 ...
TC_STATUS_OPEN = "2"          # 已开售（有实时赔率）
TC_STATUS_SUSPENDED = "3"     # 暂停销售


def _tc_fb_base_url(source_cfg: dict[str, Any]) -> str:
    return (source_cfg.get("api_base") or "https://webapi.sporttery.cn").rstrip("/")


def _tc_fb_fetch_schedule(source_cfg: dict[str, Any]) -> Any:
    """第一步：受注赛程（method=concern）。返回官方 JSON。"""
    url = f"{_tc_fb_base_url(source_cfg)}/gateway/uniform/fb/getMatchDataPageListV1.qry?method=concern"
    req = urllib.request.Request(url, headers=source_cfg.get("headers", {"User-Agent": "Mozilla/5.0"}))
    with urllib.request.urlopen(req, timeout=source_cfg.get("timeout_seconds", 20)) as r:
        return json.loads(r.read().decode("utf-8"))


def _tc_fb_fetch_general(source_cfg: dict[str, Any], match_id: str, match_status: str) -> Any:
    """第二步：单场详情（赔率）。返回官方 JSON。"""
    url = (f"{_tc_fb_base_url(source_cfg)}/gateway/uniform/fb/getMatchGeneral.qry"
           f"?matchId={match_id}&matchStatus={match_status}")
    req = urllib.request.Request(url, headers=source_cfg.get("headers", {"User-Agent": "Mozilla/5.0"}))
    with urllib.request.urlopen(req, timeout=source_cfg.get("timeout_seconds", 20)) as r:
        return json.loads(r.read().decode("utf-8"))


def _parse_tc_fb_items(raw_schedule: Any, raw_general_by_mid: dict[str, Any],
                       source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """把 fb 链路返回转成 normalize_payload 期望的格式。

    raw_schedule: getMatchDataPageListV1(method=concern) 的 JSON
    raw_general_by_mid: {matchId: getMatchGeneral 的 JSON}（已开售时 matchResultList 为空，
                        赔率实际在赛程接口的 h/d/a 字段，见下方解析）
    """
    out = []
    groups = (raw_schedule.get("value") or {}).get("matchInfoList") or []
    for g in groups:
        for m in g.get("subMatchList", []):
            biz_date = m.get("businessDate") or g.get("businessDate") or g.get("matchDate", "")
            status = str(m.get("matchStatus") or "")
            # P0 只采“已开售”（有实时赔率）；待开售跳过（无赔率），暂停/已结束跳过
            if status != TC_STATUS_OPEN:
                continue
            mid = str(m.get("matchId") or "")
            # 2026-10-06 实测：已开售比赛的胜平负赔率直接在赛程接口的 h/d/a 字段
            # （getMatchGeneral 的 matchResultList 已开售时为空，仅赛后有中奖组合）
            h = m.get("h") or ""
            d = m.get("d") or ""
            a = m.get("a") or ""
            had = {"H": str(h), "D": str(d), "A": str(a)}
            if not had["H"] or not had["D"] or not had["A"]:
                continue  # 三态不齐 → 不采
            kickoff = f"{biz_date}T{m.get('matchTime','00:00:00')}+08:00"
            out.append({
                "source_match_id": mid,
                "competition": m.get("leagueAbbName") or m.get("leagueAllName"),
                "home_team": m.get("homeTeamAbbName") or m.get("homeTeamAllName"),
                "away_team": m.get("awayTeamAbbName") or m.get("awayTeamAllName"),
                "kickoff_time_utc": kickoff,
                "market_type": "MATCH_RESULT",
                "line": None,
                "selections": [
                    {"code": "HOME", "display_name": "主胜", "odds": had["H"], "status": "OPEN"},
                    {"code": "DRAW", "display_name": "平局", "odds": had["D"], "status": "OPEN"},
                    {"code": "AWAY", "display_name": "客胜", "odds": had["A"], "status": "OPEN"},
                ],
                "source_timestamp": now_iso(),
                "source_url": "https://m.sporttery.cn/mjc/zqsj/?tab=concern",
            })
    return out


def collect(source_cfg: dict[str, Any]) -> dict[str, Any]:
    source = source_cfg["name"]
    mode = source_cfg.get("mode")
    if not source_cfg.get("enabled"):
        return {"source": source, "status": "DISABLED", "quotes_changed": 0}

    if mode == "tc_public_api":
        # 竞彩官方公开接口：https://webapi.sporttery.cn/gateway/jc/football/getMatchCalculatorV1.qry
        raw = fetch_http_json(source_cfg)
        raw_id = save_raw(source, raw, source_cfg.get("url"))
        items = _parse_tc_public_api(raw)
    elif mode == "tc_public_api_fb":
        # 竞彩官方 fb 路径（不受风控）：赛程列表即含胜平负赔率（h/d/a 字段）
        raw_schedule = _tc_fb_fetch_schedule(source_cfg)
        raw_id = save_raw(source, raw_schedule, f"{_tc_fb_base_url(source_cfg)}/gateway/uniform/fb/getMatchDataPageListV1.qry?method=concern")
        items = _parse_tc_fb_items(raw_schedule, {}, source_cfg)
    elif mode == "hg_cdp":
        # HG（皇冠）浏览器采集：连接 AdsPower/Chrome CDP → 导航页面 → JS 提取
        raw = _hg_fetch_via_cdp(source_cfg)
        raw_id = save_raw(source, raw, source_cfg.get("page_url"))
        items = _parse_hg_extract(raw, source_cfg)
    elif mode == "dafabet_cdp":
        # Dafabet 赛前 1X2 浏览器采集：AdsPower CDP → 赛前页 → Matches tab → 文本解析
        raw = _dafabet_fetch_via_cdp(source_cfg)
        raw_id = save_raw(source, raw, source_cfg.get("page_url"))
        items = _parse_dafabet_extract(raw, source_cfg)
    elif mode == "oddsapi":
        # The Odds API（公开 JSON，免费 key）：soccer h2h 全联赛 → 按博彩公司分别入库（ODDSAPI:{公司}）
        from backend.collector.opponent_oddsapi import fetch_oddsapi
        r = fetch_oddsapi(source_cfg)
        if r.get("status") in ("disabled", "error"):
            return {"source": source, "status": r["status"].upper(),
                    "reason": r.get("reason"), "quotes_changed": 0}
        raw_id = save_raw(source, r.get("items", []), API_ODDSAPI_URL)
        items = r.get("items", [])
        changed = 0
        matches = 0
        for item in items if isinstance(items, list) else []:
            try:
                # 每条 item 自带 source（ODDSAPI:{公司}），不能用统一源名覆盖
                n = normalize_payload(str(item.get("source") or source), item)
                changed += len(upsert_normalized_quote(n, raw_id))
                matches += 1
            except Exception as exc:
                print(f"collector {source} item failed: {exc}")
        return {"source": source, "status": "OK", "matches": matches,
                "quotes_changed": changed, "raw_snapshot_id": raw_id}
    elif mode == "http_json":
        raw = fetch_http_json(source_cfg)
        raw_id = save_raw(source, raw, source_cfg.get("url"))
        items = extract_json_path(raw, source_cfg.get("json_path", ""))
    elif mode == "betexplorer":
        # BetExplorer（参考源）：未来比赛列表 → 队名/时间/详情链接（盘口滞后，不产赔率）
        from backend.collector.opponent_betexplorer import fetch_betexplorer
        r = fetch_betexplorer(source_cfg)
        raw_id = save_raw(source, r.get("items", []),
                          f"https://www.betexplorer.com/soccer/?date={r.get('date','')}")
        # 无赔率 → 只留证据，不写 matches/quotes，避免污染对冲计算域
        return {"source": source, "status": (r.get("status") or "OK").upper(),
                "matches": r.get("matches", 0), "quotes_changed": 0,
                "reason": r.get("reason"), "raw_snapshot_id": raw_id,
                "note": "schedule-reference-only (no odds)"}
    elif mode == "browser_cdp":
        raw = fetch_browser(source_cfg)
        raw_id = save_raw(source, raw, source_cfg.get("url"))
        items = parse_browser_html(source, raw, source_cfg)
    else:
        raise RuntimeError(f"未知 collector mode: {mode}")

    changed = 0
    matches = 0
    for item in items if isinstance(items, list) else []:
        try:
            n = normalize_payload(source, item)
            changed += len(upsert_normalized_quote(n, raw_id))
            matches += 1
        except Exception as exc:
            # 不让单条坏数据拖垮整个采集批次
            print(f"collector {source} item failed: {exc}")
    return {"source": source, "status": "OK", "matches": matches, "quotes_changed": changed, "raw_snapshot_id": raw_id}


def process_tc_fb_schedule(source_cfg: dict[str, Any], raw_schedule: Any) -> dict[str, Any]:
    """处理 worker 已获取的赛程快照（复用，不再重新请求接口）：
    存原始 → 解析 → 标准化 → 入库。供 1s 高频 worker 检测到变化后调用。"""
    source = source_cfg["name"]
    raw_id = save_raw(
        source, raw_schedule,
        f"{_tc_fb_base_url(source_cfg)}/gateway/uniform/fb/getMatchDataPageListV1.qry?method=concern")
    items = _parse_tc_fb_items(raw_schedule, {}, source_cfg)
    changed = 0
    matches = 0
    for item in items if isinstance(items, list) else []:
        try:
            n = normalize_payload(source, item)
            changed += len(upsert_normalized_quote(n, raw_id))
            matches += 1
        except Exception as exc:
            print(f"collector {source} item failed: {exc}")
    return {"source": source, "status": "OK", "matches": matches,
            "quotes_changed": changed, "raw_snapshot_id": raw_id}


def store_dafabet_text(text: str, tz: Any, epoch: Any,
                       source_cfg: dict[str, Any]) -> dict[str, Any]:
    """Dafabet 页面文本 → 保存原始快照 → 解析 → 入库（单一路径，冷启动/驻留共用）。"""
    source = source_cfg["name"]
    raw = {"__page_text__": text, "__tz__": tz, "__epoch__": epoch,
           "__page_url__": source_cfg.get("page_url")}
    raw_id = save_raw(source, raw, source_cfg.get("page_url"))
    items = _parse_dafabet_extract(raw, source_cfg)
    changed = 0
    matches = 0
    for item in items if isinstance(items, list) else []:
        try:
            n = normalize_payload(source, item)
            changed += len(upsert_normalized_quote(n, raw_id))
            matches += 1
        except Exception as exc:
            print(f"collector {source} dafabet item failed: {exc}")
    return {"matches": matches, "quotes_changed": changed, "raw_snapshot_id": raw_id}


# ---------------------------------------------------------------------------
# HG（皇冠）浏览器采集 · CDP 通道
# 设计：AdsPower 浏览器（HG 登录态）→ CDP → 页面 JS 提取赔率 JSON
# 页面结构随 AdsPower 环境确定后，把 extract_js 写入 sources.json 即可。
# ---------------------------------------------------------------------------

def _hg_fetch_via_cdp(source_cfg: dict[str, Any]) -> Any:
    from backend.collector.cdp_client import CDPClient
    cdp_url = source_cfg.get("cdp_url")
    page_url = source_cfg.get("page_url")
    if not cdp_url or not page_url:
        raise RuntimeError("hg_cdp 模式需要 cdp_url（AdsPower 浏览器 ws 地址）和 page_url")
    client = CDPClient.connect(cdp_url)
    try:
        client.goto(page_url, wait_seconds=source_cfg.get("page_wait_seconds", 8.0))
        extract_js = source_cfg.get("extract_js")
        if not extract_js:
            # 没有提取器时，至少返回页面文本供后续开发定位
            return {"__page_text__": client.dom_text(4000)}
        body = client.evaluate(extract_js)
        import json as _json
        try:
            return _json.loads(body) if body else {"__empty__": True}
        except Exception:
            return {"__raw__": body}
    finally:
        client.close()


def _parse_hg_extract(raw: Any, source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """把 HG 提取的 JSON 转成 normalize_payload 期望格式。

    extract_js 应返回形如:
      [{"home_team": "...", "away_team": "...", "kickoff_time_utc": "...",
        "market_type": "MATCH_RESULT"|"ASIAN_HANDICAP"|"OVER_UNDER",
        "line": null|"-0.5"|"2.5",
        "selections": [{"code": "HOME"|"AWAY"|"OVER"|"UNDER"|..., "odds": "1.95", "status": "OPEN"}],
        "competition": "..."}]
    """
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        selections = []
        for sel in item.get("selections", []):
            if sel.get("odds") is None:
                continue
            selections.append({
                "code": str(sel.get("code", "")).upper(),
                "display_name": sel.get("display_name") or str(sel.get("code", "")).upper(),
                "odds": str(sel.get("odds")),
                "status": str(sel.get("status", "OPEN")).upper(),
            })
        if not selections:
            continue
        out.append({
            "source_match_id": str(item.get("source_match_id") or ""),
            "competition": item.get("competition"),
            "home_team": item.get("home_team"),
            "away_team": item.get("away_team"),
            "kickoff_time_utc": item.get("kickoff_time_utc"),
            "market_type": str(item.get("market_type", "MATCH_RESULT")).upper(),
            "line": item.get("line"),
            "selections": selections,
            "source_timestamp": item.get("source_timestamp") or now_iso(),
            "source_url": source_cfg.get("page_url"),
        })
    return out


# ---------------------------------------------------------------------------
# Dafabet 赛前 1X2 浏览器采集 · CDP 通道（AdsPower）
# 已验证（2026-10-05）：
#   · sports.dafabet.com/en/sports/240-FOOT 可渲染赛前足球列表（Matches tab）
#   · 1X2 三列 DOM 顺序 = 主 / 客 / 平（与竞彩的主/平/客不同，务必勿混）
#   · 单场标准行文本结构：
#       <主队> / <客队> / Draw / Today|Tomorrow HH:MM / ... / More /
#       HDP主盘 HDP主赔 HDP客盘 HDP客赔 o大 大赔 u小 小赔 1X2主 1X2客 1X2平
# ---------------------------------------------------------------------------

API_ODDSAPI_URL = "https://api.the-odds-api.com/v4/sports/soccer/odds/"
_DF_NAV_CLICK_JS = (
    "(function(){var els=Array.from(document.querySelectorAll('button'))"
    ".filter(function(e){return /^Matches$/i.test((e.innerText||'').trim())&&e.offsetParent!==null;});"
    "if(els.length){els[0].click();return 'ok';}return 'notfound';})()"
)

_DF_TZ_JS = (
    "(function(){var d=new Date();return JSON.stringify({tz:Intl.DateTimeFormat().resolvedOptions().timeZone,"
    "epoch:d.getTime(),iso:d.toString()});})()"
)


def _dafabet_fetch_via_cdp(source_cfg: dict[str, Any]) -> dict[str, Any]:
    """启动 AdsPower 环境 → CDP → 导航赛前页 → 切 Matches → 返回页面文本+时区。
    页面加载不稳定（偶发白屏）：每次 attempt 干净重启环境，
    先用稳定的 live 页暖场，再导航赛前页，最多 attempts 次。
    """
    import urllib.request as _ur
    from backend.collector.adspower_api import load_token, api_get
    from backend.collector.cdp_client import CDPClient

    user_id = source_cfg.get("adspower_user_id", "k1ho30ir")
    page_url = source_cfg.get("page_url", "https://sports.dafabet.com/en/sports/240-FOOT")
    warm_url = "https://sports.dafabet.com/en/live/sport/240-FOOT"
    attempts = int(source_cfg.get("attempts", 4))
    wait_s = float(source_cfg.get("page_wait_seconds", 15.0))

    last_err = None
    for _attempt in range(attempts):
        try:
            # 干净重启：先停旧实例再启动
            api_get(f"/api/v1/browser/stop?user_id={user_id}", load_token())
            time.sleep(2)
            r = api_get(f"/api/v1/browser/start?user_id={user_id}", load_token())
            if r.get("code") != 0:
                raise RuntimeError(f"adspower start failed: {r.get('msg')}")
            debug_port = r["data"]["debug_port"]
            time.sleep(4)
            pages = json.loads(_ur.urlopen(f"http://127.0.0.1:{debug_port}/json/list", timeout=10).read())
            page_ws = next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page")
            client = CDPClient.connect(page_ws)
            try:
                # 暖场：live 页稳定渲染
                client.goto(warm_url, wait_seconds=10)
                time.sleep(3)
                # 正式：赛前页
                client.goto(page_url, wait_seconds=wait_s)
                client.evaluate(_DF_NAV_CLICK_JS)
                time.sleep(6)
                text = client.dom_text(30000)
                if not text:
                    time.sleep(8)
                    text = client.dom_text(30000)
                if not text:
                    for _ in range(4):
                        client.evaluate("window.scrollBy(0, 700)")
                        time.sleep(1.5)
                    text = client.dom_text(30000)
                tz_info = client.evaluate(_DF_TZ_JS)
            finally:
                client.close()
            if not text:
                last_err = "empty page text"
                time.sleep(5)
                continue
            tz_data = {}
            try:
                tz_data = json.loads(tz_info or "{}")
            except Exception:
                pass
            return {"__page_text__": text, "__tz__": tz_data.get("tz"),
                    "__epoch__": tz_data.get("epoch"), "__page_url__": page_url}
        except Exception as exc:
            last_err = str(exc)
            time.sleep(5)
    raise RuntimeError(f"dafabet_cdp fetch failed after {attempts} attempts: {last_err}")


def _parse_dafabet_extract(raw: dict[str, Any], source_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """把 Dafabet 赛前页文本解析成 normalize_payload 期望格式（MATCH_RESULT）。
    文本行结构（每行一个 DOM 元素）：
        <联赛头> / <主队> / <客队> / Draw / Today|Tomorrow HH:MM / ... / More /
        HDP主盘 主赔 HDP客盘 客赔 o大 大赔 u小 小赔 1X2主 1X2客 1X2平
    1X2 顺序 = 主 / 客 / 平（Dafabet 特有，勿按竞彩主/平/客解析）
    """
    text = raw.get("__page_text__", "")
    if not text:
        return []
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    tz = raw.get("__tz__")
    epoch_ms = raw.get("__epoch__")
    out = []
    i = 0
    n = len(lines)
    while i < n - 2:
        a, b, c = lines[i], lines[i + 1], lines[i + 2]
        if c == "Draw" and a not in ("Draw", "vs") and b not in ("Draw", "vs"):
            # 标准行起点：主队/客队/Draw
            home, away = a, b
            kick_text = ""
            j = i + 3
            while j < n and j < i + 12:
                if "Today" in lines[j] or "Tomorrow" in lines[j]:
                    kick_text = lines[j]
                    break
                j += 1
            # 找 More 之后跟 11 个值
            more_idx = -1
            for k in range(j, min(i + 30, n)):
                if lines[k] == "More":
                    more_idx = k
                    break
            if more_idx >= 0 and more_idx + 11 < n:
                block = lines[more_idx + 1: more_idx + 12]
                # block: [HDP盘,HDP赔,HDP盘,HDP赔,O大,大赔,O小,小赔,1X2主,1X2客,1X2平]
                hdp_h, hdp_a, ou_o, ou_u = block[0], block[2], block[4], block[6]
                hdp_h_odds, hdp_a_odds = block[1], block[3]
                ou_o_odds, ou_u_odds = block[5], block[7]
                x12 = block[8:11]  # 主/客/平
                kickoff_utc = _df_kickoff_to_utc(kick_text, tz, epoch_ms)
                if kickoff_utc:
                    sels = []
                    for code, name, val in (("HOME", home, x12[0]),
                                            ("AWAY", away, x12[1]),
                                            ("DRAW", "Draw", x12[2])):
                        if val not in ("-", "", "None", None) and _is_dec(val):
                            sels.append({"code": code, "display_name": name,
                                         "odds": val, "status": "OPEN"})
                    if len(sels) == 3:
                        out.append({
                            "source_match_id": f"df:{home}|{away}",
                            "competition": lines[i - 1] if i > 0 and "," in lines[i - 1] else None,
                            "home_team": home,
                            "away_team": away,
                            "kickoff_time_utc": kickoff_utc,
                            "market_type": "MATCH_RESULT",
                            "line": None,
                            "selections": sels,
                            "source_timestamp": now_iso(),
                            "source_url": source_cfg.get("page_url"),
                        })
                    # 亚洲让球（五态结算）：主队让球盘 line=hdp_h，主/客两向
                    # 支持四分一网格（-0.25/-0.75）与 Dafabet 拆分线（"0,-0.5" → -0.25）
                    hdp_line = _normalize_hdp_line(str(hdp_h).strip()) if hdp_h not in ("-", "", "None", None) else None
                    if (hdp_line is not None and hdp_a not in ("-", "", "None", None)
                            and _is_dec(hdp_h_odds) and _is_dec(hdp_a_odds)):
                        out.append({
                            "source_match_id": f"df:{home}|{away}",
                            "competition": lines[i - 1] if i > 0 and "," in lines[i - 1] else None,
                            "home_team": home,
                            "away_team": away,
                            "kickoff_time_utc": kickoff_utc,
                            "market_type": "ASIAN_HANDICAP",
                            "line": hdp_line,
                            "selections": [
                                {"code": "HOME", "display_name": f"{home} {hdp_h}",
                                 "odds": hdp_h_odds, "status": "OPEN"},
                                {"code": "AWAY", "display_name": f"{away} {hdp_a}",
                                 "odds": hdp_a_odds, "status": "OPEN"},
                            ],
                            "source_timestamp": now_iso(),
                            "source_url": source_cfg.get("page_url"),
                        })
                    # 大小球（P1 提前接入）：主队视角 o大/u小（全场总进球盘）
                    # block[4]=大球盘口 block[5]=大球赔率 block[6]=小球盘口 block[7]=小球赔率
                    ou_line = str(ou_o).strip()
                    if (ou_line not in ("-", "", "None", None)
                            and ou_u not in ("-", "", "None", None)
                            and _is_dec(ou_o_odds) and _is_dec(ou_u_odds)):
                        out.append({
                            "source_match_id": f"df:{home}|{away}",
                            "competition": lines[i - 1] if i > 0 and "," in lines[i - 1] else None,
                            "home_team": home,
                            "away_team": away,
                            "kickoff_time_utc": kickoff_utc,
                            "market_type": "OVER_UNDER",
                            "line": ou_line,
                            "selections": [
                                {"code": "OVER", "display_name": f"大 {ou_line}",
                                 "odds": ou_o_odds, "status": "OPEN"},
                                {"code": "UNDER", "display_name": f"小 {ou_line}",
                                 "odds": ou_u_odds, "status": "OPEN"},
                            ],
                            "source_timestamp": now_iso(),
                            "source_url": source_cfg.get("page_url"),
                        })
        i += 1
    return out


# 四分一网格：-0.25 / -0.5 / -0.75 / 0.25 ……
_QUARTER_LINE = re.compile(r"^[+-]?\d+(\.25|\.5|\.75)?$")
# Dafabet 拆分线："0,-0.5" / "-0.5,-1"（逗号分隔两个相邻半盘）
_SPLIT_LINE = re.compile(r"^([+-]?\d+\.5),([+-]?\d+\.5)$")


def _normalize_hdp_line(raw: str) -> str | None:
    """Dafabet HDP 盘文本 → 四分一网格均值字符串；无法解析返回 None。"""
    from decimal import Decimal as _D
    text = raw.replace(" ", "")
    if _QUARTER_LINE.match(text):
        return text
    m = _SPLIT_LINE.match(text)
    if not m:
        return None
    try:
        left, right = _D(m.group(1)), _D(m.group(2))
    except Exception:
        return None
    # 两个半盘必须同号且相邻（差 0.5）
    if (left < 0 < right) or (right < 0 < left):
        return None
    if abs(left - right) != _D("0.5"):
        return None
    mean = (left + right) / 2
    # 均值必须落在四分一网格
    if mean * 4 != (mean * 4).to_integral_value():
        return None
    return format(mean, "f")


def _is_dec(v: str) -> bool:
    try:
        return float(v) > 1
    except (TypeError, ValueError):
        return False


def _df_kickoff_to_utc(kick_text: str, tz: str | None, epoch_ms: Any) -> str | None:
    """Today/Tomorrow HH:MM（浏览器本地时区）→ UTC ISO。
    无 tz 时按 UTC 处理（保守：仅用于匹配辅助，队名是主匹配键）。
    """
    import re
    if not kick_text:
        return None
    m = re.search(r"(Today|Tomorrow)\s+(\d{1,2}):(\d{2})", kick_text)
    if not m:
        return None
    day_flag, hh, mm = m.group(1), int(m.group(2)), int(m.group(3))
    # 浏览器本地当前时间
    if epoch_ms:
        local_now = datetime.fromtimestamp(epoch_ms / 1000, timezone.utc)  # epoch 是绝对时刻
    else:
        local_now = datetime.now(timezone.utc)
    # 用 tz 推算本地日历日
    if tz and epoch_ms:
        import zoneinfo
        try:
            z = zoneinfo.ZoneInfo(tz)
            local_dt = datetime.fromtimestamp(epoch_ms / 1000, z)
        except Exception:
            local_dt = local_now
    else:
        local_dt = local_now
    day_delta = 0 if day_flag == "Today" else 1
    target_local = local_dt.replace(hour=hh, minute=mm, second=0, microsecond=0) \
        + timedelta(days=day_delta)
    if tz and epoch_ms:
        try:
            import zoneinfo
            z = zoneinfo.ZoneInfo(tz)
            return target_local.astimezone(timezone.utc).isoformat()
        except Exception:
            pass
    return target_local.astimezone(timezone.utc).isoformat()
