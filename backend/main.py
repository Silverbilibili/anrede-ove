from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend import realtime
from backend.match_engine import match_all as match_hg_tc
from backend.db import fetchall, fetchone, init_db, row_json, seed_capabilities
from backend.collector.collector import collect
from backend.engine.engine import (
    CONFIG_VERSION, RULE_VERSION, authoritative_calculate, D,
)
from backend.plan_generator import generate_plans, solve_parlay
from backend.parlay_builder import (
    build_bet_slip, load_selections_from_db, available_parlay_types,
    refresh_slip_validity, slip_to_dict, SelectionItem,
    PARLAY_TYPES, SYSTEM_PARLAYS, D as PB_D,
)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config" / "sources.json"
STATIC_DIR = ROOT / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_capabilities()
    # 启动 1s 高频采集 + SSE 推送 worker
    realtime.init_worker(tc_cfg, hg_cfg)
    yield
    realtime.stop_workers()


app = FastAPI(title="Football Hedge P0.8", version="0.8.0", lifespan=lifespan)

@app.get("/parlay.html")
def parlay_page():
    return FileResponse(os.path.join(os.path.dirname(__file__), "..", "static", "parlay.html"))


# V4.2: Ensure JSON responses declare UTF-8 charset
from fastapi.responses import JSONResponse as _JSONResponse
class _UTF8JSONResponse(_JSONResponse):
    media_type = "application/json; charset=utf-8"
app.router.default_response_class = _UTF8JSONResponse
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class PreviewBet(BaseModel):
    selection_code: str
    odds: str
    stake: str


class PreviewRequest(BaseModel):
    states: list[str] = Field(min_length=1)
    bets: list[PreviewBet] = Field(min_length=1)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def tc_cfg() -> dict[str, Any]:
    """返回 TC（竞彩官方）数据源配置，供实时 worker 使用。"""
    cfg = load_config()
    return next((x for x in cfg.get("sources", []) if x["name"].upper() == "TC"), None)


def hg_cfg() -> dict[str, Any] | None:
    """HG：仅接本机已登录 Chrome CDP；直连，不存储认证材料，不主动登录。"""
    cfg = load_config()
    found = next((x for x in cfg.get("sources", []) if str(x.get("name", "")).upper() == "HG"), None)
    found = dict(found or {})
    enabled = os.environ.get("HG_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    found.update({
        "name": "HG",
        "enabled": enabled,
        "transport": "cdp-direct",
        "use_proxy": False,
        "require_v2": False,
        "cdp_url": os.environ.get("HG_CDP_URL", found.get("cdp_url", "http://127.0.0.1:9222")),
        "page_url": os.environ.get("HG_PAGE_URL", found.get("page_url", "https://m829.crw066.com/")),
        "host": found.get("host", "m829.crw066.com"),
        "page_hosts": ["hga050.com", "crw066.com"],
        "data_hosts": ["crw066.com"],
        "transform_paths": ["/transform.php"],
    })
    for k in ("username", "password", "cookie", "token", "authorization", "proxy"):
        found.pop(k, None)
    return found if enabled else None


# --- External HG collector cache (feed by POST /api/v1/hg/matches) ---
_HG_EXTERNAL_CACHE: dict[str, Any] = {"matches": [], "uid": "", "ver": "", "template": ""}


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/plans")
def plans_page():
    return FileResponse(STATIC_DIR / "plans.html")


@app.get("/simulator")
def simulator_page():
    return FileResponse(STATIC_DIR / "simulator.html")


@app.get("/parlay")
def parlay_page():
    return FileResponse(STATIC_DIR / "parlay.html")


@app.get("/api/v1/plans/all")
def plans_all():
    """全部串关方案（2串1 + 3串1）。无 AH 让球盘数据时返回空列表与原因。"""
    try:
        return generate_plans()
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/plans/parlay2")
def plans_parlay2(budget: str = "1000"):
    """2串1 混合对冲方案列表。"""
    from decimal import Decimal as _D
    try:
        return generate_plans(budget=_D(budget))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/v1/plans/atoms")
def plan_atoms(plan_id: str = ""):
    """P0.8 #20：方案的投注原子 + 门票（可追溯）。"""
    atoms = fetchall(
        "SELECT * FROM bet_atoms WHERE plan_id=? ORDER BY created_at", (plan_id,)) if plan_id else []
    tickets = fetchall(
        "SELECT * FROM tickets WHERE plan_id=? ORDER BY created_at DESC LIMIT 5", (plan_id,)) if plan_id else []
        # HG-TC matching via match_engine
    hg_tc_matched = match_hg_tc(hg_matches, tc_matches, threshold=0.55) if hg_matches and tc_matches else []
    return {"plan_id": plan_id, "tickets": tickets, "atoms": atoms}


@app.get("/api/v1/plans/versions")
def plan_versions(plan_id: str = ""):
    """P0.8 串关版本链：某方案的全部 ticket 版本（含 SUPERSEDED）+ 水位迭代。

    用于回答：哪个赔率版本变化导致方案 V2 替代 V1。
    """
    tickets = fetchall(
        """SELECT ticket_id, version, status, total_stake, worst_pnl, rebate_total,
                  signature, superseded_at, created_at
           FROM tickets WHERE plan_id=? ORDER BY version""", (plan_id,)) if plan_id else []
    waters = fetchall(
        """SELECT match_id, changed_source, changed_selection, from_quote_version,
                  to_quote_version, old_odds, new_odds, old_opportunity_version_id,
                  new_opportunity_version_id, changed_at
           FROM water_iterations
           WHERE old_opportunity_version_id=? OR new_opportunity_version_id=?
           ORDER BY changed_at""",
        (plan_id, plan_id)) if plan_id else []
    return {"plan_id": plan_id, "tickets": tickets, "water_iterations": waters}


@app.get("/api/v1/rebate-panel")
def rebate_panel():
    """返奖率仪表：每场未开赛跨端比赛的 TC 返奖率 / 各对手源返奖率 / 最优组合返奖率。

    返奖率 = 1 / Σ(1/赔率)。庄家抽水 = 1 - 返奖率；组合 >100% 才有套利。
    数据实时从库内最新有效报价计算，不含任何演示值。
    """
    from decimal import Decimal as _D, getcontext
    getcontext().prec = 20

    rows = fetchall(
        """SELECT m.id, m.home_team, m.away_team, m.kickoff_time_utc, mk.market_type, s.code,
                  q.source, q.odds_decimal, q.quote_version
           FROM matches m
           JOIN markets mk ON mk.canonical_match_id=m.id
           JOIN quotes q ON q.market_id=mk.id
           JOIN selections s ON q.selection_id=s.id
           WHERE m.status='SCHEDULED' AND mk.market_type='MATCH_RESULT' AND q.is_valid=1
           ORDER BY m.kickoff_time_utc, m.id, q.source, q.quote_version DESC""")

    by_match: dict[str, dict] = {}
    for r in rows:
        mm = by_match.setdefault(r["id"], {
            "match_id": r["id"], "home": r["home_team"], "away": r["away_team"],
        "matched": hg_tc_matched,
            "kickoff_time_utc": r["kickoff_time_utc"], "sources": {}, "tc": {}})
        src_map = mm["sources"].setdefault(r["source"], {})
        # first-wins = 最新版本（SQL 已按 quote_version DESC）
        if r["code"] not in src_map:
            src_map[r["code"]] = _D(r["odds_decimal"])
        if r["source"] == "TC" and r["code"] not in mm["tc"]:
            mm["tc"][r["code"]] = _D(r["odds_decimal"])

    out = []
    for mm in by_match.values():
        if len(mm["tc"]) < 3:
            continue  # TC 三态不全，跳过
        tc_sum = sum(_D(1) / o for o in mm["tc"].values())
        item = {
            "match_id": mm["match_id"], "home": mm["home"], "away": mm["away"],
            "kickoff_time_utc": mm["kickoff_time_utc"],
            "tc_quote_count": len(mm["tc"]),
            "tc_return_rate": str((_D(1) / tc_sum).quantize(_D("0.001"))),
            "tc_margin": str((tc_sum - 1).quantize(_D("0.001"))),
            "opponents": [], "best_combo": None,
        }
        # 每个对手源：返奖率
        for src, odds in mm["sources"].items():
            if src == "TC" or len(odds) < 3:
                continue
            s = sum(_D(1) / o for o in odds.values())
            item["opponents"].append({
                "source": src,
                "return_rate": str((_D(1) / s).quantize(_D("0.001"))),
                "sum_inv": str(s.quantize(_D("0.0001"))),
            })
        # 最优组合：TC 一态 + 另两态各自取最优外围源（跨源可行，与引擎 v6 口径一致）
        best = None
        all_opp: dict[str, _D] = {}  # 每态在全部对手源中的最高赔率
        for src, odds in mm["sources"].items():
            if src == "TC" or len(odds) < 1:
                continue
            for c, o in odds.items():
                if c not in all_opp or o > all_opp[c]:
                    all_opp[c] = o
        best_opp_src: dict[str, str] = {}
        for src, odds in mm["sources"].items():
            if src == "TC":
                continue
            for c, o in odds.items():
                if c in all_opp and o == all_opp[c] and c not in best_opp_src:
                    best_opp_src[c] = src
        for tc_code in ("HOME", "DRAW", "AWAY"):
            others = [c for c in ("HOME", "DRAW", "AWAY") if c != tc_code]
            if tc_code not in mm["tc"] or any(c not in all_opp for c in others):
                continue
            s = _D(1) / mm["tc"][tc_code] + sum(_D(1) / all_opp[c] for c in others)
            combo = {"tc_selection": tc_code,
                     "opponent_sources": {c: best_opp_src.get(c, "?") for c in others},
                     "combo_sum": str(s.quantize(_D("0.0001"))),
                     "combo_return_rate": str((_D(1) / s).quantize(_D("0.001")))}
            if best is None or s < _D(best["combo_sum"]):
                best = combo
        item["best_combo"] = best
        out.append(item)
    return {"panel": out, "note": "返奖率=1/Σ(1/赔率)，>100% 才有套利；组合=TC一态+外围另两态"}


def _norm_team_name(v: Any) -> str:
    import re
    s = str(v or "").lower().strip()
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "tottenham": "tottenham hotspur",
        "spurs": "tottenham hotspur",
        "psg": "paris saint-germain",
    }
    s = aliases.get(s, s)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s)


def _merge_hg_into_tc(tc_feed: dict[str, Any] | None, hg_feed: dict[str, Any] | None) -> dict[str, Any] | None:
    if not tc_feed:
        return None
    out = dict(tc_feed)
    tc_matches = [dict(m) for m in (tc_feed.get("matches") or [])]
    hg_matches = (hg_feed or {}).get("matches") or []
    hg_by_key = {}
    for hm in hg_matches:
        key = (_norm_team_name(hm.get("home")), _norm_team_name(hm.get("away")))
        if key[0] and key[1]:
            hg_by_key[key] = hm
    matched = 0
    for m in tc_matches:
        key = (_norm_team_name(m.get("home")), _norm_team_name(m.get("away")))
        hm = hg_by_key.get(key)
        if hm:
            q = hm.get("quotes") or {}
            if all(c in q and q[c].get("odds") for c in ("HOME", "DRAW", "AWAY")):
                m["hg_quotes"] = q
                m["hg_match_id"] = hm.get("match_id")
                m["hg_updated_at"] = hm.get("updated_at")
                matched += 1
    out["matches"] = tc_matches
    out["sources"] = {
        "TC": {
            "status": tc_feed.get("source_status"),
            "match_count": len(tc_matches),
            "generated_at": tc_feed.get("generated_at"),
        },
        "HG": {
            "status": (hg_feed or {}).get("source_status", "WAITING_CDP"),
            "match_count": (hg_feed or {}).get("match_count", 0),
            "matched_to_tc": matched,
            "generated_at": (hg_feed or {}).get("generated_at"),
            "transform_endpoint": (getattr(realtime, "hg_worker", None).status().get("last_transform_url")
                                   if getattr(realtime, "hg_worker", None) else None),
        },
    }
    out["source"] = "TC+HG"
    out["source_status"] = "CONNECTED" if (hg_feed or {}).get("source_status") == "CONNECTED" else "PARTIAL"
    note = out.get("market_note", "")
    out["market_note"] = note + "；HG：仅展示当前已登录 CDP 会话实际收到并识别的 1X2，与 TC 同场比赛按主客队标准化匹配。"
    return out


def _load_parsed_leagues():
    # Reliable structured league list (region -> leagues with ids) parsed from get_league_list_All.
    try:
        import os as _os
        candidates = [
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "1", "league_parsed.json"),
            "C:/1/league_parsed.json",
        ]
        for p in candidates:
            if _os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                leagues = []
                for reg in data or []:
                    for lg in reg.get("leagues", []):
                        if lg.get("id"):
                            leagues.append({"id": str(lg["id"]), "name": lg.get("name", ""), "region": reg.get("region", "")})
                return leagues
    except Exception:
        return []
    return []


def _hg_league_ids() -> list[str]:
    return [str(l["id"]) for l in _load_parsed_leagues()]


def _hg_league_count() -> int:
    return len(_hg_league_ids())


@app.get("/api/v1/real/feed")
def real_feed():
    """双源独立 feed：TC 竞彩 + HG 皇冠，互不影响。

    即使 TC=0，HG 赛事也独立返回；matched 字段只在两队都对上时才填充。
    """
    now_utc = datetime.now(timezone.utc)

    # --- TC matches ---
    tc_matches: list[dict] = []
    cached_tc = realtime.worker.snapshot() if realtime.worker else None
    if cached_tc and cached_tc.get("matches"):
        tc_matches = cached_tc["matches"]
        tc_status = cached_tc.get("source_status", "CONNECTED")
        tc_generated = cached_tc.get("generated_at")
    else:
        now_iso = now_utc.isoformat()
        rows = fetchall(
            """SELECT m.id, m.competition, m.home_team, m.away_team, m.kickoff_time_utc,
                      s.code, q.odds_decimal, q.quote_version, q.source_status,
                      q.source_timestamp, q.received_at, q.source_url, mk.market_type
               FROM matches m
               JOIN markets mk ON mk.canonical_match_id=m.id
               JOIN quotes q ON q.market_id=mk.id
               JOIN selections s ON q.selection_id=s.id
               WHERE m.status='SCHEDULED' AND q.is_valid=1 AND m.kickoff_time_utc > ?
               ORDER BY m.kickoff_time_utc, m.id, q.quote_version DESC""",
            (now_iso,))
        by_match: dict[str, dict] = {}
        for r in rows:
            mm = by_match.setdefault(r["id"], {
                "match_id": r["id"], "competition": r["competition"],
                "home": r["home_team"], "away": r["away_team"],
                "kickoff_time_utc": r["kickoff_time_utc"],
                "selections": {}, "updated_at": None, "source_url": r["source_url"]})
            if r["market_type"] and r["code"]:
                mm[f"_mt_{r['code']}"] = r["market_type"]
            if r["code"] not in mm["selections"]:
                mm["selections"][r["code"]] = {
                    "odds": r["odds_decimal"], "quote_version": r["quote_version"],
                    "source_status": r["source_status"],
                    "source_timestamp": r["source_timestamp"]}
            if mm["updated_at"] is None or str(r["received_at"]) > str(mm["updated_at"]):
                mm["updated_at"] = r["received_at"]
        for mm in by_match.values():
            sel = mm["selections"]
            # Group selections by market_type (V4.2 multi-market)
            quotes_by_mt: dict[str, dict] = {}
            for code, qdata in sel.items():
                mt = mm.get(f"_mt_{code}", "MATCH_RESULT")
                qm = quotes_by_mt.setdefault(mt, {})
                qm[code] = {"odds": qdata["odds"], "source_status": qdata["source_status"]}
            # Require at least a valid 1X2 set
            if not all(c in quotes_by_mt.get("MATCH_RESULT", {}) for c in ("HOME", "DRAW", "AWAY")):
                continue
            match_entry = {
                "match_id": mm["match_id"], "competition": mm["competition"],
                "home": mm["home"], "away": mm["away"],
                "kickoff_time_utc": mm["kickoff_time_utc"],
                "quotes": quotes_by_mt.get("MATCH_RESULT", {}),
                "markets": quotes_by_mt,
                "updated_at": mm["updated_at"], "source": "TC"}
            tc_matches.append(match_entry)
        tc_status = "CONNECTED" if tc_matches else "NO_DATA"
        tc_generated = now_iso

    # --- Helper: filter special bets (not real matches) ---
    def _is_special_bet(m: dict) -> bool:
        """Filter out HG special bets (aggregated, fantasy, special markets)."""
        comp = str(m.get("competition", "") or "")
        home = str(m.get("home", "") or "")
        away = str(m.get("away", "") or "")
        # Competition-based
        if "特别投注" in comp or "特别" in comp:
            return True
        if "奇幻赛事" in comp or "奇幻" in comp:
            return True
        if "Fantasy" in comp:
            return True
        # Team name-based (aggregated bets use generic names)
        if home in ("主场", "客场", "主队", "客队") or away in ("主场", "客场", "主队", "客队"):
            return True
        # Only "主场/客场" known home/away patterns
        if (home == "主场" and away == "客场") or (home == "客场" and away == "主场"):
            return True
        return False

    # --- Helper: parse extra_odds raw tags into market entries ---
    def _extra_odds_to_markets(extra: dict) -> dict:
        """Convert BetRadar IOR/RATIO tags into structured markets.
        
        IOR naming: IOR_{PREFIX}{H/C/N/O/E} where prefix indicates market:
          M=1X2, R=AH, OU=OverUnder, EO=EvenOdd, 
          HR=HalfTimeAH, HOU=HalfTimeOU, HM=HalfTime1X2,
          RC=CNROU? (corner), RG=GL (goal line), RE=RE (red card?),
          TS=TOTS (total shots), XR=Extra time AH, XOU=Extra time OU
        """
        if not extra:
            return {}
        result = {}
        # Scan all IOR_* keys and group by prefix
        for key, val in extra.items():
            val = str(val).strip()
            if not val or not key.startswith("IOR_"):
                continue
            suffix = key[4:]  # after IOR_
            # Determine market type from suffix
            if suffix in ("MH", "MC", "MN"):
                mk = result.setdefault("1X2", {})
                if suffix == "MH": mk["HOME"] = {"odds": val}
                elif suffix == "MC": mk["AWAY"] = {"odds": val}
                elif suffix == "MN": mk["DRAW"] = {"odds": val}
            elif suffix in ("RH", "RC"):
                mk = result.setdefault("AH", {})
                ratio_key = "RATIO_R"
                line = str(extra.get(ratio_key, "")).strip()
                if suffix == "RH": mk["HOME"] = {"odds": val, "line": line}
                elif suffix == "RC": mk["AWAY"] = {"odds": val, "line": line}
            elif suffix in ("OUH", "OUC"):
                mk = result.setdefault("OU", {})
                ratio_o = str(extra.get("RATIO_OUO", "")).strip()
                ratio_u = str(extra.get("RATIO_OUU", "")).strip()
                line = ratio_o or ratio_u
                if suffix == "OUH": mk["OVER"] = {"odds": val, "line": line}
                elif suffix == "OUC": mk["UNDER"] = {"odds": val, "line": line}
            elif suffix in ("EOO", "EOE"):
                mk = result.setdefault("EO", {})
                if suffix == "EOO": mk["ODD"] = {"odds": val}
                elif suffix == "EOE": mk["EVEN"] = {"odds": val}
            elif suffix in ("HRH", "HRC"):
                mk = result.setdefault("HTOU_HR", {})
                line = str(extra.get("RATIO_HR", "")).strip()
                if suffix == "HRH": mk["HOME"] = {"odds": val, "line": line}
                elif suffix == "HRC": mk["AWAY"] = {"odds": val, "line": line}
            elif suffix in ("HOUH", "HOUC"):
                mk = result.setdefault("HTOU", {})
                line = str(extra.get("RATIO_HOUO", extra.get("RATIO_HOUU", ""))).strip()
                if suffix == "HOUH": mk["OVER"] = {"odds": val, "line": line}
                elif suffix == "HOUC": mk["UNDER"] = {"odds": val, "line": line}
            elif suffix in ("HMH", "HMC", "HMN"):
                mk = result.setdefault("HT1X2", {})
                if suffix == "HMH": mk["HOME"] = {"odds": val}
                elif suffix == "HMC": mk["AWAY"] = {"odds": val}
                elif suffix == "HMN": mk["DRAW"] = {"odds": val}
            elif suffix in ("RCORH", "RCORC"):
                mk = result.setdefault("CNROU", {})
                line = str(extra.get("RATIO_RCO", "")).strip()
                if suffix == "RCORH": mk["OVER"] = {"odds": val, "line": line}
                elif suffix == "RCORC": mk["UNDER"] = {"odds": val, "line": line}
            elif suffix in ("RGH", "RGC"):
                mk = result.setdefault("GL", {})
                line = str(extra.get("RATIO_RGO", "")).strip()
                if suffix == "RGH": mk["OVER"] = {"odds": val, "line": line}
                elif suffix == "RGC": mk["UNDER"] = {"odds": val, "line": line}
            elif suffix in ("TSY", "TSN"):
                mk = result.setdefault("TOTS", {})
                if suffix == "TSY": mk["YES"] = {"odds": val}
                elif suffix == "TSN": mk["NO"] = {"odds": val}
            elif suffix in ("REH", "REC"):
                mk = result.setdefault("RE", {})
                line = str(extra.get("RATIO_RE", "")).strip()
                if suffix == "REH": mk["HOME"] = {"odds": val, "line": line}
                elif suffix == "REC": mk["AWAY"] = {"odds": val, "line": line}
            # Keep accumulating raw tags
        return result
    
    # --- HG matches: LIVE CDP worker first, then HTTP cache, then seed file ---
    hg_matches: list[dict] = []
    hg_status = "DISCONNECTED"
    hg_generated = None
    hg_extra: dict[str, Any] = {}
    hg_http_obj = getattr(realtime, "hg_http", None)
    hg_worker_obj = getattr(realtime, "hg_worker", None)
    
    # Priority 1: Live CDP worker (real-time data, wide match list)
    if hg_worker_obj and hg_worker_obj.snapshot():
        hg_snap = hg_worker_obj.snapshot()
        live_matches = hg_snap.get("matches", [])
        if live_matches:
            hg_matches = live_matches
            hg_status = hg_snap.get("source_status", "CONNECTED")
            hg_generated = hg_snap.get("generated_at")
            # Parse extra_odds into full market structures for all CDP matches
            # Also fallback: parse raw_tags into extra_odds if extra_odds is missing
            for m in hg_matches:
                extra = m.get("extra_odds", {})
                if not extra:
                    # Try raw_tags as fallback source for IOR/RATIO data
                    raw = m.get("raw_tags", {})
                    if raw and isinstance(raw, dict):
                        extra = {k: v for k, v in raw.items() if k.startswith("IOR_") or k.startswith("RATIO_")}
                        m["extra_odds"] = extra
                if extra and isinstance(extra, dict):
                    parsed = _extra_odds_to_markets(extra)
                    if parsed:
                        cdp_mkts = m.get("markets", {}) or {}
                        for mt, data in parsed.items():
                            if mt not in cdp_mkts:
                                cdp_mkts[mt] = data
                        m["markets"] = cdp_mkts
    
    # Priority 2: HTTP collector cache — enrich CDP matches with multi-market data
    if hg_http_obj and hg_http_obj.snapshot():
        hg_http_snap = hg_http_obj.snapshot()
        http_matches = hg_http_snap.get("matches", [])
        if http_matches:
            # Build lookup by match_id + norm(home,away)
            def _mk(k: tuple) -> tuple:
                return (k[0].lower().strip() if k[0] else "", k[1].lower().strip() if k[1] else "")
            http_lookup: dict[tuple, dict] = {}
            for hm in http_matches:
                hk = _mk((hm.get("home",""), hm.get("away","")))
                mid = hm.get("match_id","")
                if mid: http_lookup[(mid, "", "")] = hm
                if hk[0] and hk[1]: http_lookup[hk] = hm
            # Enrich CDP matches with HTTP markets
            for m in (hg_matches or []):
                # Try match_id lookup first
                mid = m.get("match_id","")
                enriched = http_lookup.get((mid, "", ""))
                if not enriched:
                    hk = _mk((m.get("home",""), m.get("away","")))
                    enriched = http_lookup.get(hk)
                if enriched:
                    # Merge markets: HTTP has richer market data
                    http_mkts = enriched.get("markets", {})
                    cdp_mkts = m.get("markets", {})
                    if http_mkts and len(http_mkts) > len(cdp_mkts):
                        m["markets"] = http_mkts
                    # Also merge extra_odds
                    http_extra = enriched.get("extra_odds", {})
                    cdp_extra = m.get("extra_odds", {})
                    if http_extra:
                        if cdp_extra:
                            cdp_extra.update(http_extra)
                        else:
                            m["extra_odds"] = http_extra
                    # Merge raw_tags
                    http_tags = enriched.get("raw_tags", {})
                    if http_tags:
                        m.setdefault("raw_tags", {}).update(http_tags)
            # Add HTTP-only matches not in CDP
            cdp_ids = {m.get("match_id","") for m in (hg_matches or [])}
            cdp_keys = {_mk((m.get("home",""), m.get("away",""))) for m in (hg_matches or [])}
            for hm in http_matches:
                mid = hm.get("match_id","")
                hk = _mk((hm.get("home",""), hm.get("away","")))
                if mid not in cdp_ids and hk not in cdp_keys:
                    hg_matches.append(hm)
            st = hg_http_obj.status()
            hg_extra = {
                "stale_count": st.get("cache_stale_count", 0),
                "cache_source": st.get("cache_source"),
                "last_valid_event_at": st.get("last_valid_event_at"),
                "last_db_commit_at": st.get("last_db_commit_at"),
                "latency_p50_ms": st.get("latency_p50_ms"),
                "latency_p95_ms": st.get("latency_p95_ms"),
                "latency_p99_ms": st.get("latency_p99_ms"),
                "last_error": st.get("last_error"),
            }
    elif not hg_matches and hg_http_obj and hg_http_obj.snapshot():
        hg_snap = hg_http_obj.snapshot()
        hg_matches = hg_snap.get("matches", [])
        hg_status = hg_snap.get("source_status", "CONNECTED")
        hg_generated = hg_snap.get("generated_at")
        st = hg_http_obj.status()
        hg_extra = {
            "stale_count": st.get("cache_stale_count", 0),
            "cache_source": st.get("cache_source"),
            "last_valid_event_at": st.get("last_valid_event_at"),
            "last_db_commit_at": st.get("last_db_commit_at"),
            "latency_p50_ms": st.get("latency_p50_ms"),
            "latency_p95_ms": st.get("latency_p95_ms"),
            "latency_p99_ms": st.get("latency_p99_ms"),
            "last_error": st.get("last_error"),
        }

    # Fallback to local seed file ONLY when no live/CDP/HTTP data
    # Check hg_http cache first — if HTTP poller has matches, prefer them over stale seed
    all_stale = all(m.get("stale") or m.get("cache") for m in hg_matches) if hg_matches else True
    http_has_fresh = False
    if hg_http_obj and hg_http_obj.snapshot():
        http_snap = hg_http_obj.snapshot()
        http_matches_cur = http_snap.get("matches", [])
        if http_matches_cur and not all(m.get("stale") or m.get("cache") for m in http_matches_cur):
            http_has_fresh = True
            if not hg_matches or all_stale:
                hg_matches = http_matches_cur
                hg_status = "HTTP"
                hg_generated = http_snap.get("generated_at")
    
    if (not hg_matches or all_stale) and not http_has_fresh:
        seed_path = ROOT / "data" / "hg_seed_feed.json"
        if seed_path.exists():
            try:
                seed_data = json.loads(seed_path.read_text(encoding="utf-8"))
                hg_matches = seed_data.get("matches", [])
                hg_status = "SEEDED"
                hg_generated = seed_data.get("generated_at")
            except Exception:
                pass

    # --- Filter special bets (aggregated/fantasy) ---
    hg_matches = [m for m in hg_matches if not _is_special_bet(m)]

    # --- Timestamp freshness check: tag stale matches & filter ancient ones ---
    _now_utc = datetime.now(timezone.utc)
    _fresh_threshold = _now_utc - timedelta(hours=6)  # data older than 6h is stale
    _ancient_threshold = _now_utc - timedelta(hours=24)  # data older than 24h is ancient
    _fresh_count = 0
    _stale_count = 0
    _ancient_count = 0
    for m in hg_matches:
        updated_str = m.get("updated_at") or m.get("generated_at") or ""
        try:
            updated_dt = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
            if updated_dt.tzinfo is None:
                updated_dt = updated_dt.replace(tzinfo=timezone.utc)
        except Exception:
            updated_dt = None
        if updated_dt:
            age = _now_utc - updated_dt
            if age > timedelta(hours=24):
                m["stale"] = True
                m["cache"] = True
                m["data_age_hours"] = round(age.total_seconds() / 3600, 1)
                _ancient_count += 1
            elif age > timedelta(hours=6):
                m["stale"] = True
                m["data_age_hours"] = round(age.total_seconds() / 3600, 1)
                _stale_count += 1
            else:
                m["stale"] = False
                m["data_age_hours"] = round(age.total_seconds() / 3600, 1)
                _fresh_count += 1
        else:
            # No timestamp = assume stale
            m["stale"] = True
            m["cache"] = True
            _stale_count += 1
    
    # Filter: remove ancient matches (>24h old), keep fresh + stale (for display with warning)
    # But if ALL matches would be removed, keep stale ones (avoid empty feed)
    _fresh_and_stale = [m for m in hg_matches if not m.get("data_age_hours", 999) or m.get("data_age_hours", 999) <= 24]
    if _fresh_and_stale:
        hg_matches = _fresh_and_stale
    # else: keep all (even ancient) to avoid empty feed

    # --- Matching by normalized team name ---
    def norm(s: str) -> str:
        return _norm_team_name(s or "")

    hg_by_key = {}
    for hm in hg_matches:
        k = (norm(hm.get("home")), norm(hm.get("away")))
        if k[0] and k[1]:
            hg_by_key[k] = hm
    tc_by_key = {}
    for tm in tc_matches:
        k = (norm(tm.get("home")), norm(tm.get("away")))
        if k[0] and k[1]:
            tc_by_key[k] = tm

    matched = []
    matched_hg_ids = set()
    for tm in tc_matches:
        k = (norm(tm.get("home")), norm(tm.get("away")))
        hm = hg_by_key.get(k)
        if hm:
            matched.append({
                "tc_match_id": tm["match_id"], "hg_match_id": hm["match_id"],
                "competition": tm.get("competition") or hm.get("competition"),
                "home": tm.get("home"), "away": tm.get("away"),
                "tc_quotes": tm.get("quotes"), "hg_quotes": hm.get("quotes"),
            })
            matched_hg_ids.add(hm["match_id"])

    unmatched_hg = [m for m in hg_matches if m["match_id"] not in matched_hg_ids]

    # Combined backward-compat list: TC matches with hg_quotes attached
    # Build combined match list: ALL HG matches (primary) + TC matches without HG counterpart
    hg_match_ids = set()
    combined_matches = []
    
    # First: all HG matches (primary data source)
    for hm in hg_matches:
        m = dict(hm)
        m["source"] = hm.get("source", "HG")
        # Match to TC for additional data
        k = (norm(hm.get("home")), norm(hm.get("away")))
        tc_match = tc_by_key.get(k)  # TC lookup by team name
        if tc_match:
            m["tc_quotes"] = tc_match.get("quotes")
            m["tc_match_id"] = tc_match.get("match_id")
        hg_match_ids.add(hm.get("match_id", ""))
        combined_matches.append(m)
    
    # Then: TC matches not already covered by HG
    for tm in tc_matches:
        k = (norm(tm.get("home")), norm(tm.get("away")))
        hm = hg_by_key.get(k)
        if hm:
            continue  # already added via HG
        m = dict(tm)
        if hm:
            m["hg_quotes"] = hm.get("quotes")
            m["hg_match_id"] = hm.get("match_id")
        combined_matches.append(m)


    # --- V4.2 DAFABET multi-market quotes (AH/OU from DB) ---
    dafabet_quotes: list[dict] = []
    try:
        dab_rows = fetchall(
            """SELECT m.id, m.competition, m.home_team, m.away_team, m.kickoff_time_utc,
                      mk.market_type, mk.line, s.code, s.display_name,
                      q.odds_decimal, q.source, q.source_status, q.is_valid,
                      q.quote_version, q.received_at
               FROM matches m
               JOIN markets mk ON mk.canonical_match_id=m.id
               JOIN quotes q ON q.market_id=mk.id
               JOIN selections s ON q.selection_id=s.id
               WHERE m.status='SCHEDULED' AND q.is_valid=1
                     AND mk.market_type IN ('ASIAN_HANDICAP','OVER_UNDER')
               ORDER BY m.kickoff_time_utc, m.id, q.quote_version DESC""")
        dab_by_match: dict[str, dict] = {}
        for r in dab_rows:
            dm = dab_by_match.setdefault(r["id"], {
                "match_id": r["id"], "competition": r["competition"],
                "home": r["home_team"], "away": r["away_team"],
                "kickoff_time_utc": r["kickoff_time_utc"],
                "markets": {}})
            mt = r["market_type"]
            mt_front = "AH" if mt == "ASIAN_HANDICAP" else "OU" if mt == "OVER_UNDER" else mt
            mt_entry = dm["markets"].setdefault(mt_front, {
                "type": mt_front, "db_type": mt, "line": r["line"],
                "selections": {}})
            if r["code"] not in mt_entry["selections"]:
                mt_entry["selections"][r["code"]] = {
                    "odds": str(r["odds_decimal"]),
                    "display_name": r["display_name"] or r["code"],
                    "source": r["source"], "source_status": r["source_status"]}
        dafabet_quotes = list(dab_by_match.values())
    except Exception as e:
        log.warning(f"DAFABET multi-market query failed: {e}")
    # HG-only feed: external collector + internal CDP/HTTP
    # Also merge external ingest cache (filter special bets)
    external_hg = [m for m in (_HG_EXTERNAL_CACHE.get("matches") or []) if not _is_special_bet(m)]
    all_hg = external_hg + hg_matches
    out = {
        "data_mode": "real",
        "generated_at": now_utc.isoformat(),
        "source": "HG",
        "source_status": "CONNECTED" if (all_hg or tc_matches) else "NO_DATA",
        "match_count": len(all_hg),
        "matches": all_hg,
        "hg_matches": all_hg,
        "league_count": _hg_league_count(),
        "league_ids": _hg_league_ids(),
        "leagues": _load_parsed_leagues(),
    }
    return out


@app.get("/api/v1/stream")
async def stream(request: Request):
    """SSE 实时推送：竞彩官方变盘即广播，前端无需轮询。"""
    return await realtime.sse_endpoint(request)


@app.get("/api/v1/realtime-status")
def realtime_status():
    """高频采集状态：TC + HG CDP + HG HTTP + SSE。"""
    out = realtime.worker.status() if realtime.worker else {"running": False}
    out["hg"] = realtime.hg_worker.status() if getattr(realtime, "hg_worker", None) else {"enabled": False}
    out["hg_http"] = realtime.hg_http.status() if getattr(realtime, "hg_http", None) else {"enabled": False}
    return out


@app.get("/api/v1/health")

@app.get("/api/v1/hg-template")
def hg_template():
    """Debug: get current HG post template for AH/OU experiments."""
    import json
    rt = realtime.hg_http
    if not rt:
        return {"error": "HG collector not running"}
    return json.loads(json.dumps({
        "bootstrapped": rt._bootstrapped,
        "post_template": rt._post_template,
        "uid": rt._uid,
        "ver": getattr(rt, "_ver", ""),
        "session_expired": rt.session_expired,
    }, default=str))


# ── HG External Collector API ──
# GET  config → get uid/ver/template for collector to use
# POST config → set uid/ver/template from collector
# POST matches → push parsed match data into cache

@app.get("/api/v1/hg/config")
def hg_get_config():
    """Return current HG config (uid, ver, template) for external collector."""
    c = dict(_HG_EXTERNAL_CACHE)
    # Fill from internal collector if available
    if not c.get("uid"):
        rt = getattr(realtime, "hg_http", None)
        if rt:
            c["uid"] = getattr(rt, "_uid", "")
            c["ver"] = getattr(rt, "_ver", "")
            c["template"] = getattr(rt, "_post_template", "")
    return {"uid": c.get("uid", ""), "ver": c.get("ver", ""), "template": c.get("template", ""),
            "matches_cached": len(c.get("matches", []))}


@app.post("/api/v1/hg/config")
async def hg_set_config(request: Request):
    """Set HG config (uid, ver, template) for external collector."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if data.get("uid"):
        _HG_EXTERNAL_CACHE["uid"] = str(data["uid"])
    if data.get("ver"):
        _HG_EXTERNAL_CACHE["ver"] = str(data["ver"])
    if data.get("template"):
        _HG_EXTERNAL_CACHE["template"] = str(data["template"])
    # Also save uid to file for persistence
    try:
        uid_file = ROOT / "runtime" / "hg_uid.txt"
        uid_file.parent.mkdir(parents=True, exist_ok=True)
        uid_file.write_text(str(data.get("uid", _HG_EXTERNAL_CACHE.get("uid", ""))), encoding="utf-8")
    except Exception:
        pass
    return {"ok": True, "uid": _HG_EXTERNAL_CACHE.get("uid", ""),
            "ver": _HG_EXTERNAL_CACHE.get("ver", ""),
            "template_saved": bool(_HG_EXTERNAL_CACHE.get("template"))}


@app.post("/api/v1/hg/matches")
async def hg_ingest_matches(request: Request):
    """External collector pushes HG match data (parsed XML from get_game_list or getDataMT)."""
    try:
        data = await request.json()
    except Exception:
        return {"error": "Invalid JSON", "ok": False}
    matches = data.get("matches") or data.get("data") or []
    if isinstance(matches, list):
        _HG_EXTERNAL_CACHE["matches"] = matches
        _HG_EXTERNAL_CACHE["generated_at"] = datetime.now(timezone.utc).isoformat()
        # Also save uid if provided
        if data.get("uid"):
            _HG_EXTERNAL_CACHE["uid"] = str(data["uid"])
        return {"ok": True, "match_count": len(matches),
                "generated_at": _HG_EXTERNAL_CACHE["generated_at"]}
    return {"error": "No matches list found", "ok": False}

def health():
    return {"status": "ok", "rule_version": RULE_VERSION, "config_version": CONFIG_VERSION,
            "title": "Football Hedge P0.8"}


@app.get("/api/v1/dashboard")
def dashboard():
    matches = fetchone("SELECT COUNT(*) AS c FROM matches")["c"]
    opportunities = fetchone("SELECT COUNT(*) AS c FROM opportunities WHERE status='VALID'")["c"]
    # B4 修复：不再按 expires_at 过滤。报价只信 source_status=CLOSED/SUSPENDED。
    active = opportunities
    raw = fetchone("SELECT COUNT(*) AS c FROM raw_snapshots")["c"]
    quote_count = fetchone("SELECT COUNT(*) AS c FROM quotes")["c"]
    return {
        "matches": matches,
        "valid_opportunities": opportunities,
        "active_opportunities": active,
        "raw_snapshots": raw,
        "quotes": quote_count,
    }


@app.get("/api/v1/matches")
def matches(limit: int = Query(100, ge=1, le=500)):
    return fetchall("SELECT * FROM matches ORDER BY kickoff_time_utc ASC LIMIT ?", (limit,))


@app.get("/api/v1/matches/{match_id}")
def match_detail(match_id: str):
    match = fetchone("SELECT * FROM matches WHERE id=?", (match_id,))
    if not match:
        raise HTTPException(404, "match not found")
    markets = fetchall("SELECT * FROM markets WHERE canonical_match_id=? ORDER BY market_type, line", (match_id,))
    for m in markets:
        m["selections"] = fetchall(
            """SELECT s.*, q.odds_decimal, q.quote_version, q.source, q.source_timestamp, q.is_valid
               FROM selections s
               LEFT JOIN quotes q ON q.selection_id=s.id
               WHERE s.market_id=?
               ORDER BY q.quote_version DESC""", (m["id"],))
    return {"match": match, "markets": markets}


@app.get("/api/v1/prospects")
def market_prospects(match_id: str = Query(...)):
    """枚举该场 MATCH_RESULT 市场所有「含 TC 腿」的下注组合（P0 锚 TC），
    按 Σ(1/赔率) 升序返回 Top 30 + 统计：每个组合怎么买、返奖率、还差多少破线。"""
    import itertools
    rows = fetchall(
        """SELECT q.source, s.code, q.odds_decimal, q.quote_version
           FROM markets mk JOIN quotes q ON q.market_id=mk.id
           JOIN selections s ON q.selection_id=s.id
           WHERE mk.canonical_match_id=? AND mk.market_type='MATCH_RESULT' AND q.is_valid=1
           ORDER BY q.source, q.quote_version DESC""", (match_id,))
    if not rows:
        return {"match_id": match_id, "error": "no_quotes", "combos": []}

    # 按 (source, code) 只保留最新报价版本（quote_version 最大），否则 TC 多版本
    # 混入枚举 → 组合爆炸（曾实测 878 万组合 / 107 秒）
    best_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        k = (r["source"], r["code"])
        if k not in best_by_key or r["quote_version"] > best_by_key[k]["quote_version"]:
            best_by_key[k] = r
    rows = list(best_by_key.values())

    per_code_options: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        per_code_options.setdefault(r["code"], []).append(r)
    for code, opts in per_code_options.items():
        best_opp: dict[str, dict[str, Any]] = {}
        for o in opts:
            if o["source"] == "TC":
                continue
            if o["source"] not in best_opp or D(o["odds_decimal"]) > D(best_opp[o["source"]]["odds_decimal"]):
                best_opp[o["source"]] = o
        tc_list = [o for o in opts if o["source"] == "TC"]
        per_code_options[code] = tc_list + list(best_opp.values())

    has_tc = any(o["source"] == "TC"
                 for opts in per_code_options.values() for o in opts)
    if not has_tc:
        return {"match_id": match_id, "error": "no_tc_leg", "combos": []}

    codes = list(per_code_options)
    combos = []
    for combo in itertools.product(*(per_code_options[c] for c in codes)):
        if not any(o["source"] == "TC" for o in combo):
            continue
        s = sum(D(1) / D(o["odds_decimal"]) for o in combo)
        legs = {o["code"]: {"source": o["source"], "odds": o["odds_decimal"],
                            "quote_version": o["quote_version"]} for o in combo}
        combos.append({
            "tc_leg": next((legs[c] for c in ("HOME", "DRAW", "AWAY")
                            if c in legs and legs[c]["source"] == "TC"), None),
            "legs": legs,
            "sum": str(s.quantize(D("0.0001"))),
            "return_rate": str((D(1) / s).quantize(D("0.001"))),
            "gap_pct": str(((s - 1) * D("100")).quantize(D("0.01"))),
        })
    combos.sort(key=lambda x: D(x["sum"]))
    below_1 = sum(1 for x in combos if D(x["sum"]) < 1)
    return {
        "match_id": match_id,
        "total_combos": len(combos),
        "below_1": below_1,
        "top": combos[:30],
    }


@app.get("/api/v1/opportunities")
def opportunities(limit: int = Query(100, ge=1, le=500)):
    """P0.8 机会列表 = 每个机会组的最新版本（不再读旧 opportunities 表）。
    字段与 opportunity_versions 对齐：id=OPPV_id、version、rule_version、status……
    执行门 final-recheck 可直接用 o.id。"""
    rows = fetchall(
        """SELECT ov.* FROM opportunity_versions ov
           WHERE (ov.opportunity_group, ov.version) IN (
               SELECT opportunity_group, MAX(version) FROM opportunity_versions GROUP BY opportunity_group)
           ORDER BY ov.created_at DESC LIMIT ?""",
        (limit,),
    )
    cat_map = {"MATCH_RESULT": "1X2", "ASIAN_HANDICAP": "AH", "OVER_UNDER": "OU"}
    for r in rows:
        r["result"] = json.loads(r.pop("result_json"))
        mt = r["result"].get("market_type")
        r["category"] = cat_map.get(mt, mt or "1X2")
        r["rebate_total"] = r["result"].get("rebate_total") or "0"
        r["covered_states"] = r["result"].get("covered_states") or []
        r["uncovered_states"] = r["result"].get("uncovered_states") or []
    return rows


@app.get("/api/v1/opportunities/{opportunity_id}")
def opportunity(opportunity_id: str):
    row = fetchone("SELECT * FROM opportunities WHERE id=?", (opportunity_id,))
    if not row:
        raise HTTPException(404, "opportunity not found")
    row["result"] = json.loads(row.pop("result_json"))
    return row


# ---------------------------------------------------------------------------
# P0.8：机会版本历史 / 水位迭代 / 源健康 / 能力矩阵 / Final Execution Gate
# ---------------------------------------------------------------------------
@app.get("/api/v1/opportunity-versions")
def opportunity_versions(match_id: str | None = None, limit: int = Query(50, ge=1, le=200)):
    sql = "SELECT * FROM opportunity_versions"
    params: tuple = ()
    if match_id:
        sql += " WHERE canonical_match_id=?"
        params = (match_id,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    rows = fetchall(sql, params + (limit,))
    for r in rows:
        r["result"] = json.loads(r.pop("result_json"))
    return rows


@app.get("/api/v1/water-iterations")
def water_iterations(match_id: str | None = None, limit: int = Query(50, ge=1, le=200)):
    sql = "SELECT * FROM water_iterations"
    params: tuple = ()
    if match_id:
        sql += " WHERE match_id=?"
        params = (match_id,)
    sql += " ORDER BY created_at DESC LIMIT ?"
    return fetchall(sql, params + (limit,))


@app.get("/api/v1/source-health")
def source_health():
    return fetchall("SELECT * FROM source_health ORDER BY source")


@app.get("/api/v1/capabilities")
def capabilities():
    rows = fetchall("SELECT * FROM market_capabilities ORDER BY canonical_market, source")
    return {"capabilities": rows}


@app.get("/api/v1/trace/{match_id}")
def trace_match(match_id: str):
    """P0.8 验收 A~F：单场比赛全链追溯（source→raw→observation→quote version→机会版本）。"""
    match = fetchone("SELECT * FROM matches WHERE id=?", (match_id,))
    if not match:
        raise HTTPException(404, "match not found")
    quotes = fetchall(
        """SELECT q.*, s.code AS selection_code, mk.market_type, mk.line
           FROM quotes q JOIN selections s ON q.selection_id=s.id
           JOIN markets mk ON q.market_id=mk.id
           WHERE q.canonical_match_id=? ORDER BY q.quote_version DESC LIMIT 30""",
        (match_id,))
    obs = fetchone(
        """SELECT COUNT(*) AS c, MAX(received_at) AS last_obs FROM source_observations
           WHERE canonical_match_id=?""", (match_id,))
    raw = fetchone(
        """SELECT COUNT(*) AS c FROM raw_snapshots rs
           JOIN source_observations so ON so.raw_snapshot_id=rs.id
           WHERE so.canonical_match_id=?""", (match_id,))
    oppv = fetchall(
        """SELECT id, opportunity_group, version, status, classification, min_pnl, total_stake,
                  coverage_status, rule_version, created_at
           FROM opportunity_versions WHERE canonical_match_id=?
           ORDER BY version""", (match_id,))
    # P0.8 #19 时间链一等数据：最新一次计算的完整延迟链
    last_calc = fetchone(
        "SELECT timechain_json FROM calculations WHERE canonical_match_id=? ORDER BY created_at DESC LIMIT 1",
        (match_id,))
    delays = {}
    if last_calc and last_calc["timechain_json"]:
        delays = json.loads(last_calc["timechain_json"])
    return {
        "match": match,
        "quotes": [{"source": q["source"], "selection": q["selection_code"],
                     "odds": q["odds_decimal"], "quote_version": q["quote_version"],
                     "source_status": q["source_status"], "received_at": q["received_at"],
                     "raw_snapshot_id": q["raw_snapshot_id"], "source_url": q["source_url"],
                     "market_type": q["market_type"], "line": q["line"]} for q in quotes],
        "evidence": {"observations": obs["c"] if obs else 0, "last_observation_at": obs["last_obs"] if obs else None,
                     "raw_snapshots": raw["c"] if raw else 0},
        "delays": delays,
        "opportunity_versions": oppv,
    }


@app.post("/api/v1/opportunity-versions/{oppv_id}/final-recheck")
def final_recheck(oppv_id: str):
    """P0.8 #21 Final Execution Gate：重新读取最新报价 → 重算 → 与机会版本比较。
    一致 → EXECUTION_READY；任何变化 → ABORT（新版本已生成）。"""
    row = fetchone("SELECT * FROM opportunity_versions WHERE id=?", (oppv_id,))
    if not row:
        raise HTTPException(404, "opportunity version not found")
    match_id = row["canonical_match_id"]
    res = authoritative_calculate(match_id)
    if res.get("status") != "COMPLETED":
        return {"gate": "ABORT", "reason": f"recalculate status={res.get('status')}"}
    fresh = fetchone(
        """SELECT * FROM opportunity_versions WHERE canonical_match_id=?
           ORDER BY created_at DESC LIMIT 1""", (match_id,))
    if not fresh:
        return {"gate": "ABORT", "reason": "no fresh version"}
    same = (fresh["min_pnl"] == row["min_pnl"] and fresh["status"] == row["status"]
            and fresh["coverage_status"] == row["coverage_status"])
    # 源状态核验：机会引用报价的源必须仍 OPEN
    src_open = fetchone(
        """SELECT COUNT(*) AS c FROM quotes q
           JOIN opportunity_versions ov ON ov.canonical_match_id=q.canonical_match_id
           WHERE ov.id=? AND q.source_status='OPEN' AND q.is_valid=1""",
        (oppv_id,))
    return {
        "gate": "EXECUTION_READY" if (same and (src_open["c"] if src_open else 0) > 0) else "ABORT",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "original_version": {"id": row["id"], "min_pnl": row["min_pnl"], "status": row["status"]},
        "fresh_version": {"id": fresh["id"], "min_pnl": fresh["min_pnl"],
                          "status": fresh["status"], "coverage": fresh["coverage_status"]},
        "sources_open": (src_open["c"] if src_open else 0) > 0,
        "reason": None if same else "quotes changed since version",
    }


@app.post("/api/v1/collector/refresh/{source}")
def refresh_source(source: str):
    cfg = load_config()
    found = next((x for x in cfg.get("sources", []) if x["name"].upper() == source.upper()), None)
    if not found:
        raise HTTPException(404, "source not configured")
    try:
        return collect(found)
    except Exception as exc:
        raise HTTPException(500, f"collector failed: {exc}")


@app.post("/api/v1/calculations/recalculate/{match_id}")
def recalculate(match_id: str, refresh: bool = False):
    """B6 修复：engine 不再自动 collect。需要新数据时由调用方显式 POST refresh。

    refresh=true 会先对所有 enabled source 跑一遍 collect，再做权威计算。
    """
    if refresh:
        cfg = load_config()
        for s in cfg.get("sources", []):
            if s.get("enabled"):
                try:
                    collect(s)
                except Exception as exc:
                    print(f"refresh {s.get('name')} failed: {exc}")
    result = authoritative_calculate(match_id)
    if result.get("status") == "NO_DATA":
        raise HTTPException(422, "no valid quote data for match")
    return result


# ---- V4.1 投注单 / 多串1 过关引擎 API ----

class ParlayBuildRequest(BaseModel):
    selections: list[dict] = Field(min_length=1)
    parlay_types: list[str] | None = None
    system_code: str | None = None
    stake_mode: str = "per_unit"
    unit_stake: str = "2"
    total_budget: str | None = None


class ParlayValidateRequest(BaseModel):
    slip_json: dict


@app.get("/api/v1/parlay/selections/available")
def parlay_selections_available(
    match_ids: str | None = None,
    source: str = "TC",
    market_type: str = "1X2",
    limit: int = Query(200, ge=1, le=500),
):
    """获取可用于投注单的真实投注项列表。

    默认返回 TC 胜平负的全部 SCHEDULED 赛事。
    source=TC|HG|ALL, market_type=1X2|AH|OU|ALL
    """
    sources = None if source.upper() == "ALL" else [source.upper()]
    # Map frontend market_type names to DB values
    _mt_map = {"1X2": "MATCH_RESULT", "AH": "ASIAN_HANDICAP", "OU": "OVER_UNDER"}
    mt_db = _mt_map.get(market_type.upper(), market_type.upper())
    market_types = None if market_type.upper() == "ALL" else [mt_db]
    mids = [m.strip() for m in match_ids.split(",") if m.strip()] if match_ids else None

    items = load_selections_from_db(
        match_ids=mids, market_types=market_types, sources=sources, limit=limit,
    )
    return {
        "count": len(items),
        "selections": [
            {
                "selection_id": s.selection_id,
                "match_id": s.match_id,
                "match_name": s.match_name,
                "competition": s.competition,
                "kickoff_time": s.kickoff_time,
                "market_type": s.market_type,
                "market_line": s.market_line,
                "selection_code": s.selection_code,
                "selection_name": s.selection_name,
                "odds": str(s.odds),
                "source": s.source,
                "quote_version": s.quote_version,
                "source_timestamp": s.source_timestamp,
                "is_valid": s.is_valid,
                "data_freshness": s.data_freshness,
            }
            for s in items
        ],
    }


@app.get("/api/v1/parlay/types/available")
def parlay_types_available(selection_count: int = Query(0, ge=0)):
    """根据已选投注项数量，返回可用的过关方式列表（含组合数）。"""
    return {"selection_count": selection_count, "types": available_parlay_types(selection_count)}


@app.post("/api/v1/parlay/build")
def parlay_build(req: ParlayBuildRequest):
    """从用户选择的投注项构建完整投注单。

    接收前端提交的 selections（投注项标识列表），
    从数据库读取最新赔率并生成组合。
    """
    if not req.selections:
        raise HTTPException(422, "至少需要一个投注项")

    # 从 DB 加载最新赔率（覆盖前端传的旧赔率）
    all_match_ids = list(set(
        s.get("match_id", "") for s in req.selections if s.get("match_id")
    ))
    if not all_match_ids:
        raise HTTPException(422, "无效的投注项：缺少 match_id")

    db_items = load_selections_from_db(match_ids=all_match_ids, sources=None, market_types=None, limit=500)

    # 按 selection_key 匹配
    db_map: dict[str, SelectionItem] = {}
    for item in db_items:
        # Normalize for frontend matching: MATCH_RESULT -> 1X2
        mt = "1X2" if item.market_type == "MATCH_RESULT" else item.market_type
        key = f"{item.match_id}:{mt}:{item.selection_code}:{item.market_line or ''}"
        # 保留最新版本
        if key in db_map:
            if item.quote_version > db_map[key].quote_version:
                db_map[key] = item
        else:
            db_map[key] = item

    selections: list[SelectionItem] = []
    missing: list[str] = []
    for s in req.selections:
        mt = s.get('market_type','1X2')
        mt = "1X2" if mt in ("1X2", "MATCH_RESULT") else mt
        key = f"{s.get('match_id')}:{mt}:{s.get('selection_code')}:{s.get('market_line') or ''}"
        db_item = db_map.get(key)
        if db_item:
            selections.append(db_item)
        else:
            missing.append(key)
            # 使用前端提交的数据作为回退（标记为 UNKNOWN 新鲜度）
            try:
                fallback = SelectionItem(
                    selection_id=s.get("selection_id", f"SEL_{uuid.uuid4().hex[:12]}"),
                    match_id=s.get("match_id", ""),
                    match_name=s.get("match_name", ""),
                    competition=s.get("competition", ""),
                    kickoff_time=s.get("kickoff_time", ""),
                    market_type=s.get("market_type", "1X2"),
                    market_line=s.get("market_line"),
                    selection_code=s.get("selection_code", ""),
                    selection_name=s.get("selection_name", ""),
                    odds=D(s.get("odds", "1.0")),
                    source=s.get("source", "UNKNOWN"),
                    quote_version=s.get("quote_version", 0),
                    source_timestamp=s.get("source_timestamp"),
                    is_valid=False,
                    data_freshness="UNKNOWN",
                    freshness_note="前端提交，未在数据库中找到最新报价",
                )
                selections.append(fallback)
            except Exception:
                pass

    unit_stake = D(req.unit_stake) if req.unit_stake else D("2")
    total_budget = D(req.total_budget) if req.total_budget else None

    slip = build_bet_slip(
        selections=selections,
        parlay_types=req.parlay_types,
        system_code=req.system_code,
        stake_mode=req.stake_mode,
        unit_stake=unit_stake,
        total_budget=total_budget,
    )

    if missing:
        slip.notes.append(f"警告：{len(missing)} 个投注项在数据库中未找到最新报价（{len(selections) - len(missing)}/{len(req.selections)} 已校验）")

    return slip_to_dict(slip)


# ── HG Parlay / 综合过关 ──

@app.get("/api/v1/parlay/hg-selections")
def parlay_hg_selections(
    competition: str | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(200, ge=1, le=1000)
):
    """Return HG matches as parlay-eligible selections with 1X2 odds.
    
    Each selection = one match x one outcome (HOME/DRAW/AWAY) with HG odds.
    Used by the 综合过关 (parlay) workflow.
    """
    # Get HG matches from real-time feed (same logic as real_feed)
    hg_worker_obj = getattr(realtime, "hg_worker", None)
    hg_http_obj = getattr(realtime, "hg_http", None)
    hg_matches: list[dict] = []
    
    # Priority 1: CDP worker (BetRadar WebSocket - most comprehensive)
    if hg_worker_obj and hg_worker_obj.snapshot():
        snap = hg_worker_obj.snapshot()
        hg_matches = snap.get("matches", [])
    
    # Priority 2: HTTP collector (HG API polling)
    if not hg_matches and hg_http_obj and hg_http_obj.snapshot():
        snap = hg_http_obj.snapshot()
        hg_matches = snap.get("matches", [])
    
    # Priority 3: Seed file
    if not hg_matches:
        seed_path = ROOT / "data" / "hg_seed_feed.json"
        if seed_path.exists():
            try:
                seed = json.loads(seed_path.read_text(encoding="utf-8"))
                hg_matches = seed.get("matches", [])
            except Exception:
                pass
    
    # Filter special bets
    if "_is_special_bet" in dir():
        hg_matches = [m for m in hg_matches if not _is_special_bet(m)]
    
    # Apply optional filters
    if competition:
        comp_lower = competition.lower()
        hg_matches = [m for m in hg_matches if comp_lower in str(m.get("competition","")).lower()]
    if search:
        q = search.lower()
        hg_matches = [m for m in hg_matches if q in (str(m.get("home","")) + " " + str(m.get("away","")) + " " + str(m.get("competition",""))).lower()]
    
    # --- Market type display names (Chinese) ---
    _MT_NAMES: dict[str, str] = {
        "1X2": "独赢", "AH": "让球", "OU": "大/小", "EO": "单/双",
        "HOU": "让球-上半场", "HTOU": "大/小-上半场", "HT1X2": "独赢-上半场",
        "CNROU": "角球数-大/小", "GL": "进球线", "RE": "净胜球",
        "TOTS": "总射门", "SCORE": "波胆", "SHOTS": "射门", "PEN": "罚牌",
    }
    
    def _sel_label(mt: str, code: str, home: str, away: str, odds_val: str) -> str:
        if mt in ("1X2", "HT1X2"):
            return {"HOME": f"{home} 主胜", "DRAW": "平局", "AWAY": f"{away} 客胜"}.get(code, code)
        if mt in ("AH", "HOU"):
            return {"HOME": f"{home}", "AWAY": f"{away}"}.get(code, code)
        if mt in ("OU", "HTOU", "CNROU", "GL"):
            return {"OVER": f"大 {odds_val}", "UNDER": f"小 {odds_val}"}.get(code, code)
        if mt == "EO":
            return {"ODD": "单", "EVEN": "双"}.get(code, code)
        if mt in ("TOTS", "SCORE", "SHOTS", "PEN"):
            return {"YES": "是", "NO": "否"}.get(code, code)
        return code
    
    # Build selection items: ALL market types from match data
    selections: list[dict] = []
    seen_match_ids: set[str] = set()
    for m in hg_matches:
        mid = str(m.get("match_id",""))
        if mid in seen_match_ids:
            continue
        seen_match_ids.add(mid)
        home = str(m.get("home",""))
        away = str(m.get("away",""))
        comp = str(m.get("competition",""))
        kickoff = str(m.get("kickoff_time",""))
        running = m.get("running", False)
        
        # 1X2 from quotes
        q = m.get("quotes", {})
        odds_h = q.get("HOME",{}).get("odds")
        odds_d = q.get("DRAW",{}).get("odds")
        odds_a = q.get("AWAY",{}).get("odds")
        if odds_h and odds_d and odds_a:
            for code, odds in [("HOME",odds_h), ("DRAW",odds_d), ("AWAY",odds_a)]:
                selections.append({
                    "selection_id": f"HG-{mid}-1X2-{code}",
                    "match_id": f"HG-{mid}", "match_name": f"{home} vs {away}",
                    "competition": comp, "kickoff_time": kickoff,
                    "market_type": "1X2", "market_display": "独赢",
                    "selection_code": code,
                    "selection_name": _sel_label("1X2", code, home, away, str(odds)),
                    "odds": str(odds), "source": "HG", "running": running,
                })
        
        # All market types from m.markets
        mkts = m.get("markets", {})
        for mt, mt_data in mkts.items():
            mt_display = _MT_NAMES.get(mt, mt)
            if isinstance(mt_data, list):
                for entry in mt_data:
                    line = entry.get("line", "")
                    for code, info in entry.items():
                        if code in ("line", "code", "type"): continue
                        if isinstance(info, dict) and info.get("odds"):
                            sel_name = _sel_label(mt, code, home, away, str(info["odds"]))
                            if line: sel_name += f" ({line})"
                            selections.append({
                                "selection_id": f"HG-{mid}-{mt}-{code}-{line}",
                                "match_id": f"HG-{mid}", "match_name": f"{home} vs {away}",
                                "competition": comp, "kickoff_time": kickoff,
                                "market_type": mt, "market_display": mt_display,
                                "market_line": line, "selection_code": code,
                                "selection_name": sel_name, "odds": str(info["odds"]),
                                "source": "HG", "running": running,
                            })
            elif isinstance(mt_data, dict):
                for code, info in mt_data.items():
                    if code in ("line", "code", "type"): continue
                    if isinstance(info, dict) and info.get("odds"):
                        selections.append({
                            "selection_id": f"HG-{mid}-{mt}-{code}",
                            "match_id": f"HG-{mid}", "match_name": f"{home} vs {away}",
                            "competition": comp, "kickoff_time": kickoff,
                            "market_type": mt, "market_display": mt_display,
                            "selection_code": code,
                            "selection_name": _sel_label(mt, code, home, away, str(info["odds"])),
                            "odds": str(info["odds"]), "source": "HG", "running": running,
                        })
        
        if len(selections) >= limit * 15:
            break
    
    return {
        "source": "HG",
        "match_count": len(seen_match_ids),
        "selection_count": len(selections),
        "selections": selections[:limit * 3],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/v1/parlay/validate")
def parlay_validate(req: ParlayValidateRequest):
    """重新校验投注单中所有投注项的赔率有效性。

    从数据库获取最新赔率，标记失效项，重新计算组合。
    """
    slip_json = req.slip_json
    selections_data = slip_json.get("selections", [])

    # 重建 SelectionItem 列表
    selections: list[SelectionItem] = []
    for sd in selections_data:
        try:
            s = SelectionItem(
                selection_id=sd.get("selection_id", ""),
                match_id=sd.get("match_id", ""),
                match_name=sd.get("match_name", ""),
                competition=sd.get("competition", ""),
                kickoff_time=sd.get("kickoff_time", ""),
                market_type=sd.get("market_type", "1X2"),
                market_line=sd.get("market_line"),
                selection_code=sd.get("selection_code", ""),
                selection_name=sd.get("selection_name", ""),
                odds=D(sd.get("odds", "1.0")),
                source=sd.get("source", "UNKNOWN"),
                quote_version=sd.get("quote_version", 0),
                source_timestamp=sd.get("source_timestamp"),
                is_valid=sd.get("is_valid", True),
                data_freshness=sd.get("data_freshness", "LIVE"),
            )
            selections.append(s)
        except Exception:
            continue

    # 重新加载最新赔率
    match_ids = list(set(s.match_id for s in selections if s.match_id))
    if match_ids:
        db_items = load_selections_from_db(match_ids=match_ids, sources=None, market_types=None, limit=500)
        db_map: dict[str, SelectionItem] = {}
        for item in db_items:
            key = f"{item.match_id}:{item.market_type}:{item.selection_code}:{item.market_line or ''}"
            if key in db_map:
                if item.quote_version > db_map[key].quote_version:
                    db_map[key] = item
            else:
                db_map[key] = item

        # 更新赔率
        for s in selections:
            key = f"{s.match_id}:{s.market_type}:{s.selection_code}:{s.market_line or ''}"
            db_item = db_map.get(key)
            if db_item:
                if db_item.odds != s.odds:
                    s.odds = db_item.odds
                    s.quote_version = db_item.quote_version
                    s.source_timestamp = db_item.source_timestamp
                    s.data_freshness = db_item.data_freshness
                    s.is_valid = db_item.is_valid

    # 重建投注单
    slip = build_bet_slip(
        selections=selections,
        parlay_types=slip_json.get("parlay_types_selected"),
        system_code=slip_json.get("system_parlay"),
        stake_mode=slip_json.get("stake_mode", "per_unit"),
        unit_stake=D(slip_json.get("unit_stake", "2")),
    )

    # 新鲜度重新校验
    slip = refresh_slip_validity(slip)

    return {
        **slip_to_dict(slip),
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "changes_detected": len(slip.notes) > 0,
    }


@app.post("/api/v1/preview/calculate")
def preview(req: PreviewRequest):
    states = req.states
    pnl = {s: Decimal("0") for s in states}
    total = Decimal("0")
    for b in req.bets:
        odds = D(b.odds)
        stake = D(b.stake)
        if odds <= 1 or stake < 0:
            raise HTTPException(422, "invalid odds/stake")
        total += stake
        for state in states:
            pnl[state] += stake * (odds - 1) if state == b.selection_code else -stake
    min_pnl = min(pnl.values())
    max_pnl = max(pnl.values())
    roi = (min_pnl / total * 100) if total else Decimal("0")
    
    # Compute HG-TC matched pairs
    matched_hg_tc = match_hg_tc(hg_matches, tc_matches, threshold=0.55) if hg_matches and tc_matches else []
    
    return {
        "mode": "PREVIEW",
        "total_stake": str(total),
        "pnl_by_state": {k: str(v) for k, v in pnl.items()},
        "min_pnl": str(min_pnl),
        "max_pnl": str(max_pnl),
        "roi": str(roi),
    }
