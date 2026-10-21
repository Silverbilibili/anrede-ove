"""权威计算引擎（P0 v2）。

改动对照 V1:
- B1: equal_stake_solution → 真对冲解析解 stake_i ∝ 1/odds_i
- B2/B3/B4: 删除 quote_stale_ms 和 expires_at=5s 纯时间过期（确认06）
- B5: 最终覆盖核验（按 Stake>0 反推覆盖事实）
- B6: engine 不再主动调 collect，由 API 触发层负责
- B7: 返点进优化函数和最终精算
- B8: 处理状态枚举齐全（INFEASIBLE/NO_DATA/COMPLETED）
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, getcontext
from pathlib import Path
from typing import Any

from backend.db import db, fetchall, fetchone, row_json
from backend.engine.asian_handicap import (
    ah_settle_state, ah_states, gd_from_state, normalize_handicap,
    settle_asian_handicap,
)

getcontext().prec = 40

ROOT = Path(__file__).resolve().parents[2]
RULE_VERSION = 7  # v7: 纯外围市场（无 TC 腿）不再写入机会版本，机会组结论以含 TC 主市场为准；v6 起跨源枚举含 TC 腿组合
CONFIG_FILE = ROOT / "config" / "sources.json"

# 配置版本从 sources.json 的 config_version 读取（统一版本，不再双处维护）
CONFIG_VERSION = 3
try:
    import json as _json
    _cv = _json.loads(CONFIG_FILE.read_text(encoding="utf-8")).get("config_version")
    if isinstance(_cv, int) and _cv > 0:
        CONFIG_VERSION = _cv
except Exception:
    pass


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def D(v: Any) -> Decimal:
    return Decimal(str(v))


# ---------------------------------------------------------------------------
# 返点配置（P0: 按 stake 固定返水，与赛果无关）
# ---------------------------------------------------------------------------
def _rebate_rate_for_source(source: str) -> Decimal:
    cfg = load_config()
    for s in cfg.get("sources", []):
        if s.get("name", "").upper() == source.upper():
            return D(s.get("rebate_rate", "0"))
    return Decimal("0")


# ---------------------------------------------------------------------------
# 平台投注限制（config/limits：TC 单注2元/2元步长/99倍上限/单张99注/2万；
# 外围默认最小10/最大5000/步长1）。source 形如 "ODDSAPI:Betfair" → 取前缀平台类。
# ---------------------------------------------------------------------------
def _platform_limit(source: str) -> dict[str, Any]:
    cfg = load_config()
    platforms = cfg.get("limits", {}).get("platforms", {})
    base = source.split(":")[0].upper()
    key = base if base in platforms else ("TC" if base == "TC" else "HG")
    lim = platforms.get(key, platforms.get("HG", {}))
    return {
        "platform": key,
        "min_stake": D(lim.get("min_stake", "1")),
        "step": D(lim.get("stake_step", "1")),
        "max_stake": D(lim["max_stake"]) if lim.get("max_stake") else None,
    }


def _tc_lim(key: str, default: Any = None) -> Any:
    cfg = load_config()
    return cfg.get("limits", {}).get("platforms", {}).get("TC", {}).get(key, default)


# ---------------------------------------------------------------------------
# 比赛快照（保留原逻辑：用最新已知有效报价，不做时间过期）
# ---------------------------------------------------------------------------
def match_snapshot(match_id: str) -> dict[str, Any] | None:
    match = fetchone("SELECT * FROM matches WHERE id=?", (match_id,))
    if not match:
        return None
    # 匹配锁（老板 #12 Match Lock）：已开赛/已结束的比赛禁止进入新计算（只做历史追踪）
    if match["status"] not in ("SCHEDULED",):
        return None
    # 兜底：kickoff 已过（matches.status 可能滞后未更新）→ 同样拒绝
    try:
        from datetime import datetime as _dt, timezone as _tz
        kt = _dt.strptime(match["kickoff_time_utc"], "%Y-%m-%dT%H:%M:%S%z")
        if kt.astimezone(_tz.utc) <= _dt.now(_tz.utc):
            return None
    except ValueError:
        pass
    market_rows = fetchall(
        "SELECT * FROM markets WHERE canonical_match_id=? AND status='OPEN'",
        (match_id,),
    )
    selected_quotes = []
    for m in market_rows:
        rows = fetchall(
            """SELECT q.*, s.code AS selection_code
               FROM quotes q JOIN selections s ON q.selection_id=s.id
               WHERE q.market_id=? AND q.is_valid=1
               ORDER BY q.selection_id, q.quote_version DESC""",
            (m["id"],),
        )
        latest_by_sel = {}
        for q in rows:
            # 跨源混算：每个 (赛果, 源) 各保留最新版本报价（TC 与 DAFABET 不互相覆盖）
            latest_by_sel.setdefault((q["selection_code"], q["source"]), q)
        selected_quotes.extend(latest_by_sel.values())
    if not selected_quotes:
        return None

    created = now_iso()
    payload = {
        "match_id": match_id,
        "match_version": match["canonical_version"],
        "quotes": [
            {
                "id": q["id"],
                "source": q["source"],
                "market_id": q["market_id"],
                "selection_id": q["selection_id"],
                "selection_code": q["selection_code"],
                "odds_decimal": q["odds_decimal"],
                "quote_version": q["quote_version"],
                "received_at": q["received_at"],
            }
            for q in selected_quotes
        ],
        "rule_version": RULE_VERSION,
        "config_version": CONFIG_VERSION,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    sid = f"SNAP_{uuid.uuid4().hex[:16]}"
    with db() as conn:
        conn.execute(
            """INSERT INTO calculation_snapshots
               (id,canonical_match_id,created_at,snapshot_hash,
                tc_quote_version,hg_quote_version,match_version,
                rule_version,config_version,status)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                sid, match_id, created, digest,
                max((q["quote_version"] for q in selected_quotes if q["source"] == "TC"), default=None),
                max((q["quote_version"] for q in selected_quotes if q["source"] == "HG"), default=None),
                match["canonical_version"], RULE_VERSION, CONFIG_VERSION, "SEALED",
            ),
        )
        for q in selected_quotes:
            conn.execute(
                """INSERT INTO calculation_snapshot_items
                   (snapshot_id,quote_id,source,market_id,selection_id,quote_version,odds_decimal)
                   VALUES (?,?,?,?,?,?,?)""",
                (sid, q["id"], q["source"], q["market_id"], q["selection_id"],
                 q["quote_version"], q["odds_decimal"]),
            )
    return {"id": sid, "payload": payload}


# ---------------------------------------------------------------------------
# 状态空间
# P0: MATCH_RESULT 三态 (HOME/DRAW/AWAY)
# v4: ASIAN_HANDICAP 五态等价态（整数盘走水/四分一拆分，按 line 生成净胜球区间）
# P1: OVER_UNDER 两态 (OVER/UNDER，半盘无走水)
# ---------------------------------------------------------------------------
def _line_to_std(line) -> str:
    """Dafabet 逗号拆分线（"0, -0.5"）→ 五态模块斜杠格式（去空格）。"""
    return str(line).replace(",", "/").replace(" ", "")


def state_space_for_market(market_type: str, selection_codes: list[str],
                           line: Any = None) -> list[str]:
    if market_type == "ASIAN_HANDICAP" and line is not None and str(line) not in ("", "None"):
        try:
            return ah_states(_line_to_std(line))
        except Exception:
            pass  # 盘口解析失败降级为两态
    if market_type == "MATCH_RESULT":
        canonical = [c for c in ["HOME", "DRAW", "AWAY"] if c in selection_codes]
        return canonical if canonical else selection_codes
    if market_type == "ASIAN_HANDICAP":
        canonical = [c for c in ["HOME", "AWAY"] if c in selection_codes]
        return canonical if canonical else selection_codes
    if market_type == "OVER_UNDER":
        canonical = [c for c in ["OVER", "UNDER"] if c in selection_codes]
        return canonical if canonical else selection_codes
    return selection_codes


# ---------------------------------------------------------------------------
# 真对冲求解器（B1 修复）
#
# 解析解: stake_i = budget * (1/odds_i) / Σ(1/odds_i)
# 若 Σ(1/odds_i) >= 1 => INFEASIBLE
# 按 step 向下取整，再用 Decimal 精确重算逐状态 PnL（含返点）
# ---------------------------------------------------------------------------
def solve_arbitrage(bets: list[dict[str, Any]],
                   states: list[str],
                   budget: Decimal,
                   step: Decimal,
                   min_stake: Decimal,
                   settle: Any = None) -> dict[str, Any] | None:
    """settle(state, bet, stake) -> 该状态下的返还（含本金）。默认：selection_code 命中全额返还。"""
    if not bets or not states:
        return None
    # P0 边界（老板 P0 定义：TC 单关 × 对手源对冲）：方案必须包含 TC 竞彩腿。
    # 纯外围价差（如 ODDSAPI×DAFABET 互套）不属 P0 范围，标记 INFEASIBLE。
    if not any(b["source"] == "TC" for b in bets):
        return {
            "status": "INFEASIBLE",
            "reason": "no_tc_leg: P0 要求对冲方案包含 TC 报价（纯外围价差不属 P0）",
            "sum_inv": None,
        }

    inv = [Decimal("1") / D(b["odds_decimal"]) for b in bets]
    sum_inv = sum(inv, Decimal("0"))

    # 无套利条件
    if sum_inv >= 1:
        return {
            "status": "INFEASIBLE",
            "reason": f"sum_implied_prob={sum_inv.quantize(Decimal('0.0001'))} >= 1",
            "sum_inv": str(sum_inv),
        }

    # 解析最优比例，按每腿平台限制取整（TC: 2元步长/上限198；外围: 步长1/最小10/最大5000）
    lims = [_platform_limit(b["source"]) for b in bets]
    ideal = [budget * inv[i] / sum_inv for i in range(len(bets))]
    stakes = []
    for i, x in enumerate(ideal):
        lim = lims[i]
        s = (x / lim["step"]).to_integral_value(rounding=ROUND_DOWN) * lim["step"]
        s = max(lim["min_stake"], s)
        if lim["max_stake"] is not None:
            s = min(lim["max_stake"], s)
        stakes.append(s)
    total = sum(stakes, Decimal("0"))

    # 取整后可能超预算，按比例缩（逐腿步长）
    if total > budget:
        scale = budget / total
        new_stakes = []
        for i, s in enumerate(stakes):
            lim = lims[i]
            s2 = (s * scale / lim["step"]).to_integral_value(rounding=ROUND_DOWN) * lim["step"]
            if s2 < lim["min_stake"]:
                s2 = lim["min_stake"]
            new_stakes.append(s2)
        stakes = new_stakes
        total = sum(stakes, Decimal("0"))

    if total <= 0:
        return {"status": "INFEASIBLE", "reason": "stakes rounded to zero", "sum_inv": str(sum_inv)}

    # 返点（P0: 按 stake 固定返水，与赛果无关）
    rebate_by_bet = [s * _rebate_rate_for_source(b["source"]) for s, b in zip(stakes, bets)]
    rebate_total = sum(rebate_by_bet, Decimal("0"))

    # 精确重算逐状态净收益：settle 返回该状态返还（含本金），减总投，加总返点
    pnl_by_state = {}
    for state in states:
        returned = Decimal("0")
        for i, b in enumerate(bets):
            if settle is not None:
                returned += settle(state, b, stakes[i])
            elif b["selection_code"] == state:
                returned += stakes[i] * D(b["odds_decimal"])
        pnl_by_state[state] = returned - total + rebate_total

    min_pnl = min(pnl_by_state.values())

    # 最终覆盖核验（B5 修复）：每个 state 是否至少有一个 active bet 返还 > 0
    covered, uncovered = [], []
    for state in states:
        any_win = any(
            stakes[i] > 0 and (
                settle(state, bets[i], stakes[i]) > 0 if settle is not None
                else bets[i]["selection_code"] == state)
            for i in range(len(bets))
        )
        (covered if any_win else uncovered).append(state)

    return {
        "status": "COMPLETED",
        "stakes": [
            {
                "quote_id": bets[i]["id"],
                "source": bets[i]["source"],
                "selection_code": bets[i]["selection_code"],
                "stake": str(stakes[i]),
                "limits": {
                    "platform": lims[i]["platform"],
                    "min_stake": str(lims[i]["min_stake"]),
                    "step": str(lims[i]["step"]),
                    "max_stake": str(lims[i]["max_stake"]) if lims[i]["max_stake"] is not None else None,
                    "compliant": stakes[i] >= lims[i]["min_stake"]
                                 and (lims[i]["max_stake"] is None or stakes[i] <= lims[i]["max_stake"])
                                 and (stakes[i] - lims[i]["min_stake"]) % lims[i]["step"] == 0,
                },
            }
            for i in range(len(bets))
        ],
        "limits_summary": {
            "total_stake": str(total),
            "total_notes": len(bets),
            "tc_max_notes_per_ticket": _tc_lim("max_notes_per_ticket"),
            "tc_max_ticket_amount": str(_tc_lim("max_ticket_amount", "20000")),
            "notes_compliant": len(bets) <= int(_tc_lim("max_notes_per_ticket", "99")),
            "amount_compliant": total <= D(_tc_lim("max_ticket_amount", "20000")),
        },
        "total_stake": str(total),
        "pnl_by_state": {k: str(v) for k, v in pnl_by_state.items()},
        "min_pnl": str(min_pnl),
        "max_pnl": str(max(pnl_by_state.values())),
        "rebate_total": str(rebate_total),
        "sum_inv": str(sum_inv),
        "covered_states": covered,
        "uncovered_states": uncovered,
        "full_coverage": len(uncovered) == 0,
    }


def _iso_seconds(start: str | None, end: str | None) -> float | None:
    """ISO8601 字符串差值（秒）。任一缺失返回 None。"""
    if not start or not end:
        return None
    from datetime import datetime
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%f%z"
        s = datetime.strptime(start, fmt)
        e = datetime.strptime(end, fmt)
        return round((e - s).total_seconds(), 4)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 权威计算入口
# ---------------------------------------------------------------------------
def _insert_opp_version(conn, match_id: str, *, calc_version: int, status: str,
                        classification: str, grade: str, total_stake: str, min_pnl: str,
                        max_pnl: str, roi: str, coverage_status: str, sum_inv: str,
                        ver_json: str, result_payload: str,
                        detected_at: str, created_at: str) -> tuple[str, Any, int, str]:
    """机会版本写入（并发安全：MAX(version)+1 + IntegrityError 重试）。

    返回 (version_id, prev_row, version, group)。
    同一 group 内版本递增；旧版标 REPLACED。scheduler/verify/API 并发计算同一场不冲突。
    """
    import sqlite3 as _sq
    for _ in range(3):
        prev = conn.execute(
            """SELECT * FROM opportunity_versions WHERE canonical_match_id=?
               ORDER BY created_at DESC LIMIT 1""", (match_id,)).fetchone()
        same_group = bool(prev and prev["status"] not in ("CLOSED", "SUPERSEDED"))
        group = prev["opportunity_group"] if same_group else f"OPP_{match_id[:12]}"
        vmax = conn.execute(
            "SELECT COALESCE(MAX(version),0) FROM opportunity_versions WHERE opportunity_group=?",
            (group,)).fetchone()[0]
        version = int(vmax) + 1
        vid = f"OPPV_{uuid.uuid4().hex[:16]}"
        try:
            conn.execute(
                """INSERT INTO opportunity_versions
                   (id,opportunity_group,version,canonical_match_id,status,classification,grade,
                    total_stake,min_pnl,max_pnl,roi,coverage_status,sum_inv,
                    rule_version,config_version,calculation_version,quote_versions_json,
                    prev_version_id,replaced_by,result_json,detected_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (vid, group, version, match_id, status, classification, grade,
                 total_stake, min_pnl, max_pnl, roi, coverage_status, sum_inv,
                 RULE_VERSION, CONFIG_VERSION, calc_version, ver_json,
                 prev["id"] if prev else None, None, result_payload,
                 detected_at, created_at),
            )
            if prev:
                conn.execute(
                    "UPDATE opportunity_versions SET status='REPLACED', replaced_by=? WHERE id=?",
                    (vid, prev["id"]),
                )
            return vid, prev, version, group
        except _sq.IntegrityError:
            continue  # 并发同写 → 重读重试
    raise RuntimeError(f"opportunity version conflict: {match_id}")


def authoritative_calculate(match_id: str) -> dict[str, Any]:
    """对指定 match_id 做权威计算。

    B6: 不再内部调 collect()，采集由 API / 触发层显式调用。
    """
    snap = match_snapshot(match_id)
    calc_started = now_iso()
    if not snap:
        return {"status": "NO_DATA", "match_id": match_id, "calc_started_at": calc_started}

    payload = snap["payload"]
    snapshot_id = snap["id"]
    quotes = payload["quotes"]

    # P0.8 计算缓存：签名 = 报价版本集 + 规则/配置版本。报价没变 → 直接命中缓存返回，
    # 不重算、不写新 calculations/机会版本（修复版本虚增 V62 与 SQLite 写放大卡顿）
    calc_sig = hashlib.sha256(json.dumps({
        "quotes": sorted(
            (q["source"], q["selection_code"], q["quote_version"], q["odds_decimal"])
            for q in quotes),
        "rule_version": RULE_VERSION,
        "config_version": CONFIG_VERSION,
    }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
    cached = fetchone(
        """SELECT result_json, calc_finished_at FROM calculations
           WHERE canonical_match_id=? AND calc_signature=?
           ORDER BY created_at DESC LIMIT 1""", (match_id, calc_sig))
    if cached:
        res = json.loads(cached["result_json"])
        res["cached"] = True
        res["cached_calc_id"] = res.get("calc_id")
        res["calc_id"] = res.get("calc_id") or ""
        return res

    grouped: dict[str, list[dict[str, Any]]] = {}
    for q in quotes:
        grouped.setdefault(q["market_id"], []).append(q)

    cfg = load_config()
    budget = D(cfg.get("engine", {}).get("default_budget", "1000"))
    step = D(cfg.get("engine", {}).get("stake_step", "1"))
    min_stake = D(cfg.get("engine", {}).get("min_stake", "1"))

    results = []
    infeasible = []
    for market_id, qs in grouped.items():
        market = fetchone("SELECT * FROM markets WHERE id=?", (market_id,))
        if not market:
            continue
        selection_codes = []
        code_by_selection_id = {}
        for q in qs:
            sel = fetchone("SELECT code FROM selections WHERE id=?", (q["selection_id"],))
            if sel:
                selection_codes.append(sel["code"])
                code_by_selection_id[q["selection_id"]] = sel["code"]
        states = state_space_for_market(market["market_type"], selection_codes,
                                        market.get("line"))
        bets = []
        for q in qs:
            code = code_by_selection_id.get(q["selection_id"])
            if code:
                bets.append({**q, "selection_code": code})

        # v6: AH 市场用五态结算命中（走水/半赢半输），其余市场沿用 selection_code 命中
        settle_fn = None
        if market["market_type"] == "ASIAN_HANDICAP":
            try:
                line_d = normalize_handicap(_line_to_std(market["line"])) if market.get("line") not in (None, "", "None") else None
                if line_d is not None:
                    settle_fn = (lambda state, bet, stake, ld=line_d:
                                 ah_settle_state(state, bet, stake, ld))
            except Exception:
                settle_fn = None

        # 跨源对冲聚合（v6，P0 边界收口）：
        # 每个赛果可选「TC 价」或「任一对手源价」（主胜买 TC、平/客买不同公司——跨平台对冲的标准做法）。
        # 枚举所有「含 ≥1 条 TC 腿」的组合，逐个求解，取 min_pnl 最优方案。
        # 不再用 best_by_code 把多源压成一个组合——那样含 TC 的组合从未被枚举。
        per_code_options: dict[str, list[dict[str, Any]]] = {}
        for b in bets:
            per_code_options.setdefault(b["selection_code"], []).append(b)
        for code, opts in per_code_options.items():
            # 每赛果：TC 全部保留；对手源按源去重取该源最高价
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
        plan = None
        best_reason = None
        best_sum_inv = None
        if has_tc:
            import itertools
            codes = list(per_code_options)
            # 全组合 = Π(每赛果选项数)；只求解含 TC 的组合（P0 边界：TC 单关必须参与）
            # 典型规模：1X2 三态 × (TC 1 + 对手 19) = 8000 组合，含 TC 约 1141，毫秒级可接受
            for combo in itertools.product(*(per_code_options[c] for c in codes)):
                if not any(o["source"] == "TC" for o in combo):
                    continue
                p = solve_arbitrage(list(combo), states, budget, step, min_stake,
                                    settle=settle_fn)
                if p is None:
                    continue
                if p["status"] == "COMPLETED":
                    if plan is None or D(p["min_pnl"]) > D(plan["min_pnl"]):
                        plan = p
                else:
                    si = p.get("sum_inv")
                    if best_reason is None or (si is not None and
                                               (best_sum_inv is None or D(si) < D(best_sum_inv))):
                        best_reason = p.get("reason")
                        best_sum_inv = si
            if plan is None and best_reason is None:
                best_reason = "no_tc_leg: 含 TC 组合全部无解"
        else:
            best_reason = "no_tc_leg: P0 要求对冲方案包含 TC 报价（该市场无 TC 报价）"

        if plan is None:
            # P0.8 #13：无解也必须留版本（NO_FEASIBLE_SOLUTION），统一在 conn 块内写入
            infeasible.append({"market_id": market_id, "reason": best_reason,
                               "sum_inv": best_sum_inv or ""})
            continue

        roi = (D(plan["min_pnl"]) / D(plan["total_stake"]) * Decimal("100")) if D(plan["total_stake"]) else Decimal("0")
        results.append({
            "market_id": market_id,
            "market_type": market["market_type"],
            "states": states,
            "stake_plan": plan["stakes"],
            "total_stake": plan["total_stake"],
            "min_pnl": plan["min_pnl"],
            "max_pnl": plan["max_pnl"],
            "rebate_total": plan["rebate_total"],
            "roi": str(roi),
            "coverage_status": "COMPLETE" if plan["full_coverage"] else "PARTIAL",
            "covered_states": plan["covered_states"],
            "uncovered_states": plan["uncovered_states"],
            "sum_inv": plan["sum_inv"],
            "settlement_authority": f"RULESET_{RULE_VERSION}",
        })

    calc_finished = now_iso()
    calc_id = f"CALC_{uuid.uuid4().hex[:16]}"
    calc_version_row = fetchone("SELECT COALESCE(MAX(calculation_version),0)+1 AS v FROM calculations")
    calc_version = int(calc_version_row["v"])

    # P0.8 #19 时间链一等数据：报价最早/最晚到达 → 计算开始 → 计算完成，逐段延迟
    recv = [q.get("received_at") for q in quotes if q.get("received_at")]
    tc = {
        "quotes_count": len(quotes),
        "oldest_quote_received_at": min(recv) if recv else None,
        "newest_quote_received_at": max(recv) if recv else None,
        "calc_started_at": calc_started,
        "calc_finished_at": calc_finished,
        "calculation_delay_s": _iso_seconds(calc_started, calc_finished),
    }
    if recv:
        tc["collection_to_calc_delay_s"] = _iso_seconds(max(recv), calc_started)
        tc["end_to_end_delay_s"] = _iso_seconds(min(recv), calc_finished)

    result = {
        "status": "COMPLETED",
        "match_id": match_id,
        "snapshot_id": snapshot_id,
        "calc_id": calc_id,
        "calc_signature": calc_sig,
        "opportunities": results,
        "infeasible_markets": infeasible,
        "timechain": tc,
    }

    with db() as conn:
        conn.execute(
            """INSERT INTO calculations
               (id,snapshot_id,canonical_match_id,status,result_json,
                calculation_version,created_at,calc_finished_at,timechain_json,calc_signature)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (calc_id, snapshot_id, match_id, "COMPLETED", row_json(result),
             calc_version, calc_started, calc_finished, row_json(tc), calc_sig),
        )
        # ---- P0.8 机会版本化 + 水位迭代（OPP group → V1/V2/...） ----
        quote_version_map = {
            f"{q['source']}:{q['selection_code']}": q["quote_version"]
            for q in payload["quotes"]
        }
        # 无解市场也写机会版本（NO_FEASIBLE_SOLUTION），保证每轮计算都有状态可追溯。
        # P0 机会必须含 TC 腿：纯外围市场（让球/大小等无 TC 报价）不占机会版本，
        # 避免其 no_tc_leg 覆盖主市场（MATCH_RESULT）的真实结论。
        for inf in infeasible:
            if (inf.get("reason") or "").startswith("no_tc_leg"):
                continue
            _insert_opp_version(
                conn, match_id, calc_version=calc_version,
                status="NO_FEASIBLE_SOLUTION", classification="NON_PROFIT", grade="D",
                total_stake="0", min_pnl="0", max_pnl="0", roi="0", coverage_status="NONE",
                sum_inv=inf.get("sum_inv", ""),
                ver_json=row_json({
                    "market_signature": inf["market_id"],
                    "quote_versions": {k: v for k, v in quote_version_map.items() if v is not None},
                    "reason": inf.get("reason", ""),
                }),
                result_payload=row_json({"reason": inf.get("reason", ""),
                                         "market_id": inf["market_id"]}),
                detected_at=calc_started, created_at=calc_finished)
        for r in results:
            min_pnl = D(r["min_pnl"])
            if min_pnl > 0:
                classification = "POSITIVE"
            elif min_pnl == 0:
                classification = "BREAK_EVEN"
            else:
                classification = "NON_PROFIT"
            # 状态枚举统一（P0.8 #13）：覆盖不全且非正收益 → INSUFFICIENT_COVERAGE
            if r["coverage_status"] != "COMPLETE" and classification != "POSITIVE":
                opp_status = "INSUFFICIENT_COVERAGE"
            elif classification == "POSITIVE" and r["coverage_status"] == "COMPLETE":
                opp_status = "POSITIVE"
            else:
                opp_status = classification
            grade = _grade_for_pmin(min_pnl)

            prev = conn.execute(
                """SELECT * FROM opportunity_versions WHERE canonical_match_id=?
                   ORDER BY created_at DESC LIMIT 1""",
                (match_id,)).fetchone()
            opp_ver_id = f"OPPV_{uuid.uuid4().hex[:16]}"
            ver_json = row_json({
                "market_signature": r["market_id"],
                "quote_versions": {
                    k: v for k, v in quote_version_map.items()
                    if v is not None
                },
            })
            opp_ver_id, prev, _version, _group = _insert_opp_version(
                conn, match_id, calc_version=calc_version,
                status=opp_status, classification=classification, grade=grade,
                total_stake=r["total_stake"], min_pnl=r["min_pnl"], max_pnl=r["max_pnl"],
                roi=r["roi"], coverage_status=r["coverage_status"], sum_inv=r["sum_inv"],
                ver_json=ver_json, result_payload=row_json(r),
                detected_at=calc_started, created_at=calc_finished)
            # 水位迭代：找出从上一版到这一版哪些报价变化，导致 Pmin 变化
            if prev:
                try:
                    prev_res = json.loads(prev["result_json"])
                    old_min_pnl = D(prev_res.get("min_pnl", "0"))
                except Exception:
                    old_min_pnl = None
                if old_min_pnl is not None and old_min_pnl != min_pnl:
                    prev_versions = {}
                    try:
                        prev_meta = json.loads(prev["quote_versions_json"])
                        prev_versions = prev_meta.get("quote_versions", {})
                    except Exception:
                        pass
                    changed_sels = []
                    for k, v in prev_versions.items():
                        if quote_version_map.get(k) != v:
                            changed_sels.append(k)
                    for k in quote_version_map:
                        if k not in prev_versions:
                            changed_sels.append(k)
                    for ck in changed_sels[:3]:
                        src, code = ck.split(":", 1)
                        old_odds = None
                        sel_row = conn.execute(
                            "SELECT q.odds_decimal FROM quotes q JOIN selections s ON q.selection_id=s.id WHERE q.source=? AND s.code=? AND q.canonical_match_id=? ORDER BY q.quote_version DESC LIMIT 1",
                            (src, code, match_id)).fetchone()
                        # 上一版赔率：取 prev 版本号对应的报价
                        pv = prev_versions.get(ck)
                        if pv is not None:
                            qr = conn.execute(
                                "SELECT q.odds_decimal FROM quotes q JOIN selections s ON q.selection_id=s.id WHERE q.source=? AND s.code=? AND q.canonical_match_id=? AND q.quote_version<=? ORDER BY q.quote_version DESC LIMIT 1",
                                (src, code, match_id, pv)).fetchone()
                            if qr:
                                old_odds = qr["odds_decimal"]
                        conn.execute(
                            """INSERT INTO water_iterations
                               (id,match_id,canonical_market_id,from_quote_version,to_quote_version,
                                changed_selection,changed_source,old_odds,new_odds,changed_at,
                                old_opportunity_version_id,new_opportunity_version_id,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (f"WI_{uuid.uuid4().hex[:16]}", match_id, r["market_id"],
                             prev_versions.get(ck), quote_version_map.get(ck),
                             code, src, old_odds,
                             sel_row["odds_decimal"] if sel_row else None,
                             calc_finished, prev["id"], opp_ver_id, calc_finished),
                        )
            # 事件（P0.8 #17 事件总线：机会生命周期事件）
            conn.execute(
                """INSERT INTO quote_events
                   (id,source,canonical_match_id,market_id,selection_id,
                    event_type,old_odds,new_odds,quote_version,received_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (f"QEV_{uuid.uuid4().hex[:16]}", "ENGINE", match_id, r["market_id"],
                 None,
                 "OPPORTUNITY_SUPERSEDED" if prev else "OPPORTUNITY_CREATED",
                 prev["id"] if prev else None, opp_ver_id, calc_version,
                 calc_finished, calc_finished),
            )
            opp_id = f"OPP_{uuid.uuid4().hex[:16]}"
            conn.execute(
                """INSERT INTO opportunities
                   (id,canonical_match_id,snapshot_id,market_signature,status,classification,grade,
                    total_stake,min_pnl,max_pnl,roi,coverage_status,
                    data_quality_status,execution_status,
                    calculation_version,config_version,rule_version,
                    detected_at,expires_at,created_at,updated_at,result_json,
                    rebate_total,covered_states,uncovered_states)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    opp_id, match_id, snapshot_id, f"{r['market_id']}",
                    "VALID", classification, grade,
                    r["total_stake"], r["min_pnl"], r["max_pnl"], r["roi"], r["coverage_status"],
                    "PASS", "MANUAL_ONLY",
                    calc_version, CONFIG_VERSION, RULE_VERSION,
                    calc_started, None, calc_started, calc_finished, row_json(r),
                    r["rebate_total"],
                    json.dumps(r["covered_states"]),
                    json.dumps(r["uncovered_states"]),
                ),
            )

    return {"status": "COMPLETED", "calculation_id": calc_id,
            "calculation_version": calc_version, **result}


# ---------------------------------------------------------------------------
# V1 补充：机会等级 / 准入 / 报价事件
# ---------------------------------------------------------------------------
def _grade_for_pmin(pmin: Decimal) -> str:
    """机会等级（模块#13）：按最小含返点净收益分级。
    A: Pmin >= 2%  B: 0.5%-2%  C: <0.5%  D: <=0
    """
    if pmin <= 0:
        return "D"
    pct = pmin / D(1000) * 100  # 默认预算 1000，转百分比
    if pct >= 2:
        return "A"
    if pct >= Decimal("0.5"):
        return "B"
    return "C"


def _record_quote_event(source: str, match_id: str, market_id: str,
                         selection_id: str, event_type: str,
                         old_odds: str | None, new_odds: str,
                         quote_version: int) -> None:
    """记录报价变化事件（模块#5）：用于连续重算与时间链。"""
    now = now_iso()
    with db() as conn:
        conn.execute(
            """INSERT INTO quote_events
               (id,source,canonical_match_id,market_id,selection_id,
                event_type,old_odds,new_odds,quote_version,received_at,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f"QEV_{uuid.uuid4().hex[:16]}", source, match_id, market_id,
             selection_id, event_type, old_odds, new_odds, quote_version, now, now),
        )


def _admission_check(quotes: list[dict]) -> dict[str, Any]:
    """计算准入（模块#6）：每个 quote 的准入结果 + 原因。
    P0 简化：只判断 odds > 1 且 source_status = OPEN。
    """
    admitted, rejected = [], []
    for q in quotes:
        try:
            odds = D(q["odds_decimal"])
        except Exception:
            rejected.append({"selection_id": q["selection_id"], "reason": "invalid_odds"})
            continue
        if odds <= 1:
            rejected.append({"selection_id": q["selection_id"], "reason": f"odds_too_low:{odds}"})
            continue
        admitted.append({"selection_id": q["selection_id"], "odds": str(odds)})
    return {"admitted": admitted, "rejected": rejected}


# ---------------------------------------------------------------------------
# V1 补充：比赛状态机（模块#8）
# ---------------------------------------------------------------------------
MATCH_STATUSES = ("SCHEDULED", "PRE_MATCH", "LIVE", "FINISHED", "CANCELLED")


def derive_match_status(kickoff_utc: str, now_utc: str | None = None) -> str:
    """P0 赛前生命周期（无独立事件源）：
    - now < kickoff-2h  → SCHEDULED
    - kickoff-2h ≤ now < kickoff → PRE_MATCH
    - kickoff ≤ now < kickoff+2h → LIVE
    - now ≥ kickoff+2h → FINISHED
    """
    from datetime import datetime, timedelta
    now = datetime.fromisoformat(now_utc or now_iso())
    kick = datetime.fromisoformat(kickoff_utc)
    if now < kick - timedelta(hours=2):
        return "SCHEDULED"
    if now < kick:
        return "PRE_MATCH"
    if now < kick + timedelta(hours=2):
        return "LIVE"
    return "FINISHED"


def refresh_match_status(match_id: str) -> str:
    """更新单场比赛状态（模块#8）。"""
    row = fetchone("SELECT kickoff_time_utc, status FROM matches WHERE id=?", (match_id,))
    if not row:
        return "UNKNOWN"
    new_status = derive_match_status(row["kickoff_time_utc"])
    if new_status != row["status"]:
        with db() as conn:
            conn.execute(
                "UPDATE matches SET status=?, status_updated_at=? WHERE id=?",
                (new_status, now_iso(), match_id),
            )
    return new_status
