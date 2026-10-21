"""串关混合对冲方案生成器（P0 扩展 · v3）

结构（参照模板：TC 2串1 + 两场 HG 受让单关）：
    TC n串1：每腿选 1~2 个赛果（复式），命中 = 各腿真实赛果都在选区内，
            命中组合的官方奖金 = 2元×连乘赔率 → 五成双 → 保底/封顶 → ×M
    HG 让球单关：每场买"未选方向"的近端受让盘，五态精确结算（任意盘口）
状态空间 = 每场净胜球桶（gd∈-4..4）笛卡尔积，逐态精确 PnL（含返点）。

受让腿五态：复用 backend.engine.asian_handicap（老板历史资产迁移模块），
    不再假设"纯+0.5 未命中即赢"——四分一/整数/半盘全部按真实盘口结算。

降关：parlay_void_settle —— 单腿无效 → 该腿赔率按 1（官方降关）；
     全部腿无效 → 整票退款。

均衡解（解析精确）：
    设 R = 目标返还（= 串关命中态最差官方返还），X_i = R / o_i，T = Σ X_i
    min 净收益 = 状态矩阵精确重算（五态 + 官方舍入 + 返点）
"""
from __future__ import annotations

import itertools
import sqlite3
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

from backend.engine.asian_handicap import normalize_handicap, settle_asian_handicap

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "football.db"

D = Decimal
CODE_CN = {"HOME": "主胜", "DRAW": "平局", "AWAY": "客胜"}

# 体彩返点：实际投注金额 × 5%（V1 文档 R19，OWNER_CONFIRMED 2026-09-09）
TC_REBATE_RATE = D("0.05")
# HG/Dafabet 返点（sources.json 配置，默认 0.005）
DF_REBATE_RATE = D("0.005")

# 每腿可选选区（复式）：单选主/客 + 双选 主+平 / 平+客（主+客无对冲腿，跳过）
_SELECTION_SETS = (("HOME",), ("AWAY",), ("HOME", "DRAW"), ("DRAW", "AWAY"))
# 选区 → 受让方向（对冲未覆盖侧；平局由受让盘自然覆盖）
_HEDGE_OF_SET = {"HOME": "AWAY", "AWAY": "HOME",
                 "HOME_DRAW": "AWAY", "DRAW_AWAY": "HOME"}
_GD_RANGE = tuple(range(-4, 5))  # 净胜球桶 -4..4

# 平台投注限制（config/limits.json 同源；TC 公开规则 + 外围默认）
import json as _json

def _load_limits() -> dict[str, Any]:
    try:
        return _json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8")).get("limits", {})
    except Exception:
        return {}

_LIM = _load_limits()
_TC_LIM = _LIM.get("platforms", {}).get("TC", {})
_HG_LIM = _LIM.get("platforms", {}).get("HG", {})
TC_MIN_STAKE = D(_TC_LIM.get("min_stake", "2"))
TC_STEP = D(_TC_LIM.get("stake_step", "2"))
TC_MAX_MULT = int(_TC_LIM.get("max_multiplier", "99"))
TC_MAX_NOTES = int(_TC_LIM.get("max_notes_per_ticket", "99"))
TC_MAX_AMOUNT = D(_TC_LIM.get("max_ticket_amount", "20000"))
HG_MIN_STAKE = D(_HG_LIM.get("min_stake", "10"))
HG_MAX_STAKE = D(_HG_LIM.get("max_stake", "5000"))
HG_STEP = D(_HG_LIM.get("stake_step", "1"))


def _sporttery_cent_round(x: Decimal) -> Decimal:
    """官方「四舍六入五成双」：只按小数点后第3位，量化至分（V1 R14）。

    例：2.5452 → 2.54（第3位=5，第2位=4 偶 → 舍）。
    """
    h = (x * 100).to_integral_value(rounding=ROUND_DOWN)
    d3 = (x * 1000).to_integral_value(rounding=ROUND_DOWN) - D("10") * h
    if d3 <= 4 or (d3 == 5 and int(h) % 2 == 0):
        return h / 100
    return (h + 1) / 100


def parlay_unit_prize(combo_odds: Decimal, multiplier: int) -> Decimal:
    """官方中奖返还（单个组合）：2元 × 连乘赔率 → 五成双舍入 → 最低2元 →
    单注上限20万 → 汇总 × 倍数 M（V1 PayoutRule：R14/R15）。"""
    raw = D("2") * combo_odds
    rounded = _sporttery_cent_round(raw)
    payable = max(D("2"), min(rounded, D("200000")))
    return payable * D(multiplier)


def parlay_void_settle(leg_odds_list: list[Decimal], void_indexes: set[int],
                       multiplier: int) -> Decimal:
    """官方降关结算（V1）：单腿无效 → 该腿赔率按 1；全部无效 → 整票退款。"""
    if len(void_indexes) == len(leg_odds_list):
        return D("2") * D(multiplier) * D(len(leg_odds_list))
    combo = D("1")
    for i, o in enumerate(leg_odds_list):
        if i in void_indexes:
            continue
        combo *= o
    return parlay_unit_prize(combo, multiplier)


def _latest_tc_odds(db: sqlite3.Connection, match_id: str) -> dict[str, tuple[Decimal, int]]:
    """该场 TC 全场胜平负最新有效报价：{selection_code: (odds, quote_version)}。"""
    rows = db.execute(
        """SELECT s.code, q.odds_decimal, q.quote_version FROM quotes q
           JOIN markets mk ON q.market_id = mk.id
           JOIN selections s ON q.selection_id = s.id
           WHERE mk.canonical_match_id=? AND mk.market_type='MATCH_RESULT'
             AND q.source='TC' AND q.is_valid=1
           ORDER BY s.code, q.quote_version DESC""",
        (match_id,)).fetchall()
    out: dict[str, tuple[Decimal, int]] = {}
    seen: set[str] = set()
    for code, odds, qv in rows:
        if code in seen:
            continue
        seen.add(code)
        out[code] = (D(odds), qv)
    return out


def _latest_ah_candidates(db: sqlite3.Connection, match_id: str) -> dict[str, list[tuple[Decimal, Decimal, int]]]:
    """该场 AH OPEN 市场最新报价：{side: [(line(主队视角), odds, quote_version), ...]}（去重）。"""
    rows = db.execute(
        """SELECT s.code, q.odds_decimal, mk.line, q.quote_version FROM quotes q
           JOIN markets mk ON q.market_id = mk.id
           JOIN selections s ON q.selection_id = s.id
           WHERE mk.canonical_match_id=? AND mk.market_type='ASIAN_HANDICAP'
             AND mk.status='OPEN' AND q.is_valid=1
           ORDER BY s.code, mk.line, q.quote_version DESC""",
        (match_id,)).fetchall()
    out: dict[str, list[tuple[Decimal, Decimal, int]]] = {}
    seen: set[tuple] = set()
    for code, odds, line, qv in rows:
        key = (code, line)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(code, []).append((_line_to_std(line), D(odds), qv))
    return out


def _pick_hedge(ah: dict[str, list[tuple[Decimal, Decimal, int]]], side: str) -> tuple[Decimal, Decimal, int] | None:
    """受让侧近端盘：side=HOME 期望主受让 +0.5（line 接近 +0.5）；
    side=AWAY 期望客受让 +0.5（主队 line 接近 -0.5）。"""
    target = D("0.5") if side == "HOME" else D("-0.5")
    best = None
    for line, odds, qv in ah.get(side, []):
        dist = abs(line - target)
        if best is None or dist < best[0]:
            best = (dist, line, odds, qv)
    return None if best is None else (best[1], best[2], best[3])


def _line_to_std(line) -> Decimal:
    """Dafabet 逗号拆分线（"0,-0.5"）→ 五态模块斜杠格式 → 四分一网格。"""
    return normalize_handicap(str(line).replace(",", "/"))


# 五态返还系数缓存：gross_return 与 stake 线性（返还 = coef × stake），
# 同一 (gd, 盘口, 赔率) 跨倍数/组合只算一次。
_SETTLE_CACHE: dict[tuple, Decimal] = {}


def _settle_coef(gd: int, line: Decimal, odds: Decimal) -> Decimal:
    key = (gd, str(line), str(odds))
    if key not in _SETTLE_CACHE:
        s = settle_asian_handicap(selection_goal_difference=gd, handicap=line,
                                  stake=D("1"), decimal_odds=odds)
        _SETTLE_CACHE[key] = s.gross_return
    return _SETTLE_CACHE[key]


def _settle_hedge(leg: dict, gd: int, stake: Decimal) -> Decimal:
    """受让腿五态精确返还（含本金）。leg: {hedge_code, hedge_line(主队视角), hedge_odds}。"""
    side = leg["hedge_code"]
    line = _line_to_std(leg["hedge_line"])
    if side == "HOME":
        sel_gd, handicap = gd, line
    else:
        sel_gd, handicap = -gd, -line
    return _settle_coef(sel_gd, handicap, leg["hedge_odds"]) * stake


def _match_info(db: sqlite3.Connection, match_id: str) -> dict[str, Any]:
    r = db.execute("SELECT home_team, away_team, competition, kickoff_time_utc FROM matches WHERE id=?",
                   (match_id,)).fetchone()
    return {"match_id": match_id, "home": r[0], "away": r[1],
            "competition": r[2], "kickoff_time_utc": r[3]}


def solve_parlay(matches: list[str], budget: Decimal = D("1000"),
                 step: Decimal = D("1"), min_stake: Decimal = D("1")) -> dict[str, Any] | None:
    """n 场串关方案（TC 复式串关 + 每场 HG 受让单关）。n=2 → 2串1，n=3 → 3串1。

    枚举每腿选区（单选主/客 + 复式 主+平 / 平+客）× 枚举倍数 M，
    取最差净收益最大者。受让腿按真实盘口五态精确结算。
    """
    db = sqlite3.connect(str(DB))
    infos = [_match_info(db, m) for m in matches]
    tc_legs = [_latest_tc_odds(db, m) for m in matches]
    ah_legs = [_latest_ah_candidates(db, m) for m in matches]
    # 数据完整性：每场 TC 需有 HOME/AWAY 报价；受让侧需有 AH 盘
    for i, tc in enumerate(tc_legs):
        missing = [c for c in ("HOME", "AWAY") if c not in tc]
        if missing:
            return {"status": "NO_LEGS", "reason": f"第{i+1}场 TC 缺 {missing} 报价"}
        if not ah_legs[i]:
            return {"status": "NO_LEGS",
                    "reason": f"第{i+1}场 {infos[i]['home']} vs {infos[i]['away']} 无 HG 让球盘数据"}

    best: dict[str, Any] | None = None
    # 每腿选区组合（复式笛卡尔积）
    set_combos = itertools.product(_SELECTION_SETS, repeat=len(matches))
    for sets in set_combos:
        legs: list[dict[str, Any]] = []
        skip = False
        for i, s in enumerate(sets):
            hedge_code = _HEDGE_OF_SET["_".join(s)]
            hedge = _pick_hedge(ah_legs[i], hedge_code)
            if hedge is None:
                skip = True  # 该受让方向无盘口，此选区组合不可行
                break
            legs.append({
                "match": infos[i],
                "selections": s,
                "tc_odds": {c: tc_legs[i][c][0] for c in s},
                "tc_qv": {c: tc_legs[i][c][1] for c in s},
                "hedge_code": hedge_code,
                "hedge_line": hedge[0],
                "hedge_odds": hedge[1],
                "hedge_qv": hedge[2],
            })
        if skip:
            continue
        plan = _solve_multiplier(matches, infos, legs, budget, step, min_stake)
        if plan is None:
            continue
        if best is None or D(plan["worst_pnl"]) > D(best["worst_pnl"]):
            best = plan
    return best


def _solve_multiplier(matches: list[str], infos: list[dict], legs: list[dict],
                      budget: Decimal, step: Decimal, min_stake: Decimal) -> dict[str, Any] | None:
    """体彩官方金额模型：TC 串关金额 = 2元×组合数×M（M∈1..50，单票≤6000）。

    最优倍数解析：worst(m) = m·δ + ε(m)，δ = R1(1-Σ1/o) - 2K。
      δ<0（非套利）→ worst 随 m 单调下降 → M=1 最优；
      δ>0（完美套利）→ worst 随 m 单调上升 → M=m_max 最优；
      |δ| 极小（1 元内）→ 中间 M 可能因取整 ε 略优 → 全枚举兜底。
    受让腿均衡 R = 串关命中态最差官方返还；五态精确重算取最差净收益最大者。
    """
    k = 1
    for l in legs:
        k *= len(l["selections"])
    combo_returns: list[Decimal] = []
    for combo in itertools.product(*(l["selections"] for l in legs)):
        combo_odds = D("1")
        for i, c in enumerate(combo):
            combo_odds *= legs[i]["tc_odds"][c]
        combo_returns.append(parlay_unit_prize(combo_odds, 1))

    # 倍数上限：平台规则（每注最多99倍）∧ 单张最多99注（组合数×M≤99）∧
    # 单张金额≤2万 ∧ 预算。组合数 k 即注数/倍；M 为倍数。
    max_m = min(TC_MAX_MULT, 50,
                TC_MAX_NOTES // k,
                int(TC_MAX_AMOUNT // (D("2") * D(k))),
                int(budget // (D("2") * D(k))))
    if max_m < 1:
        return None

    def try_m(m: int) -> dict[str, Any] | None:
        x1 = D("2") * D(k) * D(m)
        R = min(combo_returns) * D(m)
        # 受让腿按外围限制：步长、最小注、最大注
        stakes = [_floor(R / l["hedge_odds"], HG_STEP) for l in legs]
        stakes = [min(HG_MAX_STAKE, max(HG_MIN_STAKE, s)) for s in stakes]
        total = x1 + sum(stakes)
        if total > budget or x1 < TC_MIN_STAKE or any(s < HG_MIN_STAKE for s in stakes):
            return None
        return _finalize(matches, infos, legs, m, x1, stakes, total, budget)

    p1, px = try_m(1), try_m(max_m)
    if p1 is None:
        return px
    if px is None:
        return p1
    if abs(D(p1["worst_pnl"]) - D(px["worst_pnl"])) < D("2"):
        # 边界模糊（套利临界）：全枚举保证最优
        best = p1
        for m in range(2, max_m + 1):
            p = try_m(m)
            if p is not None and D(p["worst_pnl"]) > D(best["worst_pnl"]):
                best = p
        return best
    return p1 if D(p1["worst_pnl"]) > D(px["worst_pnl"]) else px


def _finalize(matches: list[str], infos: list[dict], legs: list[dict],
              multiplier: int, stake_parlay: Decimal, stakes: list[Decimal],
              total: Decimal, budget: Decimal) -> dict[str, Any]:
    """净胜球桶状态矩阵精确重算（五态受让 + 官方串关奖金 + 返点）并组装方案。"""
    n = len(matches)
    k = 1
    for l in legs:
        k *= len(l["selections"])
    rebate_tc = stake_parlay * TC_REBATE_RATE
    rebate_hg = sum(s * DF_REBATE_RATE for s in stakes)
    rebate_total = rebate_tc + rebate_hg

    # 复式组合的单注奖金（M=1），用于展示
    combo_returns: list[Decimal] = []
    for combo in itertools.product(*(l["selections"] for l in legs)):
        combo_odds = D("1")
        for i, c in enumerate(combo):
            combo_odds *= legs[i]["tc_odds"][c]
        combo_returns.append(parlay_unit_prize(combo_odds, 1))

    # 串关命中：所有腿真实赛果 ∈ 对应选区 → 命中唯一组合（H/D/A 互斥）
    def combo_of(gds: tuple) -> tuple:
        return tuple("HOME" if g >= 1 else "DRAW" if g == 0 else "AWAY" for g in gds)

    def parlay_return_on(gds: tuple) -> Decimal:
        combo = combo_of(gds)
        if not all(combo[i] in legs[i]["selections"] for i in range(n)):
            return D("0")
        combo_odds = D("1")
        for i, c in enumerate(combo):
            combo_odds *= legs[i]["tc_odds"][c]
        return parlay_unit_prize(combo_odds, multiplier)

    # 净胜球桶状态 → PnL → 按 PnL 聚合（唯一态）
    bucket: dict[str, list[tuple]] = {}
    for gds in itertools.product(_GD_RANGE, repeat=n):
        pnl = -total + rebate_total + parlay_return_on(gds)
        for i, l in enumerate(legs):
            pnl += _settle_hedge(l, gds[i], stakes[i])
        bucket.setdefault(str(pnl), []).append(gds)

    states = []
    worst = None
    for pnl_str, gd_list in bucket.items():
        pnl = D(pnl_str)
        states.append({
            "pnl": pnl_str,
            "count": len(gd_list),
            "coverage": [{"gd": list(g), "result": list(combo_of(g)),
                          "state": " × ".join(
                              f"{infos[k]['home']} {'赢' if g>=1 else '平' if g==0 else '负'} ({g:+d})"
                              for k, g in enumerate(g))}
                         for g in gd_list[:2]],
            "coverage_note": f"覆盖 {len(gd_list)}/{len(list(itertools.product(_GD_RANGE, repeat=n)))} 个净胜球桶",
        })
        if worst is None or pnl < worst:
            worst = pnl

    # Σ(1/赔率) 示意：受让全赢赔率 + 串关最大组合（仅展示用）
    parlay_odds_ref = D("1")
    for l in legs:
        parlay_odds_ref *= max(l["tc_odds"].values())
    inv_sum = D("1") / parlay_odds_ref + sum(D("1") / l["hedge_odds"] for l in legs)

    return {
        "status": "SOLVED",
        "type": f"PARLAY{n}",
        "structure": f"TC {n}串1（{multiplier}倍×2元×{'' if n==2 else ''}组合）+ {n} 场 HG 受让单关（五态）",
        "matches": [{"match_id": m, "home": i["home"], "away": i["away"]}
                    for m, i in zip(matches, infos)],
        "parlay": {"selections": [list(l["selections"]) for l in legs],
                   "stake": str(stake_parlay), "multiplier": multiplier,
                   "combos": "×".join(str(len(l["selections"])) for l in legs),
                   "unit_prize_min": str(min(combo_returns)),
                   "return_on_hit_min": str(min(combo_returns) * D(multiplier)),
                   "legs": [{"match": f"{infos[i]['home']} vs {infos[i]['away']}",
                             "picks": list(l["selections"]),
                             "odds": {c: str(l["tc_odds"][c]) for c in l["selections"]}}
                            for i, l in enumerate(legs)]},
        "hedges": [{"match_id": matches[i], "match": f"{infos[i]['home']} vs {infos[i]['away']}",
                    "hedge": l["hedge_code"], "line": str(l["hedge_line"]),
                    "odds": str(l["hedge_odds"]), "stake": str(stakes[i])}
                   for i, l in enumerate(legs)],
        "total_stake": str(total),
        "budget": str(budget),
        "limits": {
            "tc_leg": {
                "stake": str(stake_parlay), "notes": int(k) * multiplier,
                "max_notes": TC_MAX_NOTES, "max_multiplier": TC_MAX_MULT,
                "max_ticket_amount": str(TC_MAX_AMOUNT),
                "notes_compliant": int(k) * multiplier <= TC_MAX_NOTES,
                "multiplier_compliant": multiplier <= TC_MAX_MULT,
                "amount_compliant": stake_parlay <= TC_MAX_AMOUNT,
            },
            "hedge_legs": [
                {"match": f"{infos[i]['home']} vs {infos[i]['away']}", "stake": str(stakes[i]),
                 "min": str(HG_MIN_STAKE), "max": str(HG_MAX_STAKE), "step": str(HG_STEP),
                 "compliant": HG_MIN_STAKE <= stakes[i] <= HG_MAX_STAKE}
                for i in range(n)],
        },
        "sum_inv": str(inv_sum.quantize(D("0.0001"))),
        "arbitrage": inv_sum < 1,
        "rebate": {"tc": str(rebate_tc), "hg": str(rebate_hg), "total": str(rebate_total)},
        "tax_note": "单期中奖合计>1万按20%偶然所得税（V1 R20 聚合口径，需同一人同期同游戏外部上下文）",
        "worst_pnl": str(worst),
        "worst_roi": str((worst / total * 100).quantize(D("0.01"))),
        "coverage": "COMPLETE",
        "states": states,
        # P0.8 串关版本化：方案绑定的报价版本集（签名/水位迭代数据源）
        "quote_versions": [
            {"match_id": legs[i]["match"].get("match_id", f"leg{i}"),
             "source": "TC", "selection": c, "odds": str(legs[i]["tc_odds"][c]),
             "quote_version": (legs[i].get("tc_qv") or {}).get(c, 0)}
            for i in range(n) for c in legs[i]["selections"]
        ] + [
            {"match_id": legs[i]["match"].get("match_id", f"leg{i}"),
             "source": "HG", "selection": legs[i]["hedge_code"],
             "odds": str(legs[i]["hedge_odds"]), "quote_version": legs[i].get("hedge_qv", 0)}
            for i in range(n)
        ],
    }


def _floor(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(rounding=ROUND_DOWN) * step


def _plan_signature(quote_versions: list[dict[str, Any]]) -> str:
    """方案签名 = 报价版本集排序 JSON 的 md5（报价无变化 → 签名相同 → 不重写不新版本）。"""
    import hashlib
    import json as _json
    payload = sorted(
        ({"match_id": q["match_id"], "source": q["source"], "selection": q["selection"],
          "quote_version": q["quote_version"], "odds": q["odds"]} for q in quote_versions),
        key=lambda x: (x["match_id"], x["source"], x["selection"]))
    return hashlib.md5(_json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def _write_water_iterations(db: sqlite3.Connection, match_id: str, old_qvs: list[dict],
                            new_qvs: list[dict], old_ticket_id: str, new_ticket_id: str) -> None:
    """水位迭代：同一方案旧版本 → 新版本之间，逐（match, source, selection）记录报价版本变化。

    旧报价只能做历史追踪，不能继续当当前可交易报价（P0.8 规则：source 明确暂停/变化即禁止沿用）。
    """
    from datetime import datetime, timezone
    import uuid as _uuid

    old_map = {(q["match_id"], q["source"], q["selection"]): q for q in old_qvs}
    new_map = {(q["match_id"], q["source"], q["selection"]): q for q in new_qvs}
    ts = datetime.now(timezone.utc).isoformat()
    for key, nq in new_map.items():
        oq = old_map.get(key)
        # 版本号变化 OR 赔率变化 都算水位变化（真实系统报价变化必伴随 quote_version 递增，odds 比较是双保险）
        if not oq or oq["quote_version"] != nq["quote_version"] or oq["odds"] != nq["odds"]:
            db.execute(
                """INSERT INTO water_iterations
                   (id,match_id,from_quote_version,to_quote_version,changed_selection,
                    changed_source,old_odds,new_odds,changed_at,
                    old_opportunity_version_id,new_opportunity_version_id,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (_uuid.uuid4().hex[:20], match_id,
                 (oq["quote_version"] if oq else None), nq["quote_version"],
                 nq["selection"], nq["source"],
                 (oq["odds"] if oq else None), nq["odds"], ts,
                 old_ticket_id, new_ticket_id, ts))


def persist_plan(p: dict[str, Any]) -> dict[str, Any]:
    """P0.8 串关版本化：方案 → tickets 版本链 + bet_atoms。

    - 同 plan_id + 同签名（报价无变化）→ 缓存命中，返回现有 ticket（不重写）
    - 报价变化 → 新版本：旧 ticket 标记 SUPERSEDED，新 ticket version=N+1
    - 同时写 water_iterations（哪个赔率版本变化导致方案变化）
    """
    from datetime import datetime, timezone
    import uuid as _uuid

    plan_id = p["plan_id"]
    qvs: list[dict[str, Any]] = p.get("quote_versions") or []
    sig = _plan_signature(qvs)
    ts = datetime.now(timezone.utc).isoformat()
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row

    # 现有最新 ticket（同 plan_id）
    cur = db.execute(
        """SELECT ticket_id, version, signature, quote_versions_json FROM tickets
           WHERE plan_id=? ORDER BY version DESC LIMIT 1""", (plan_id,)).fetchone()
    if cur and cur["signature"] == sig:
        db.close()
        return {"ticket_id": cur["ticket_id"], "version": cur["version"], "cached": True,
                "plan_id": plan_id, "signature": sig}

    old_ticket_id = cur["ticket_id"] if cur else None
    new_version = (cur["version"] + 1) if cur else 1

    ticket_id = f"TKT_{_uuid.uuid4().hex[:12]}"
    with db:
        if old_ticket_id:
            db.execute("UPDATE tickets SET status='SUPERSEDED', superseded_at=? WHERE ticket_id=?",
                       (ts, old_ticket_id))
        db.execute("DELETE FROM bet_atoms WHERE plan_id=?", (plan_id,))
        # 门票（版本链）
        db.execute(
            """INSERT INTO tickets
               (ticket_id,plan_id,opportunity_version_id,status,total_stake,worst_pnl,
                worst_roi,rebate_total,budget,created_at,version,signature,
                quote_versions_json,superseded_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ticket_id, plan_id, None,
             "EXECUTION_READY" if p.get("arbitrage") else "NO_ARBITRAGE_NOT_RECOMMENDED",
             p["total_stake"], p["worst_pnl"], p.get("worst_roi", ""),
             str(p.get("rebate", {}).get("total", "0")), p.get("budget", ""), ts,
             new_version, sig,
             _json_dumps(qvs), None))
        # 投注原子：1) TC 串关票（单张原子记录，代表整张串票）
        pl = p["parlay"]
        db.execute(
            """INSERT INTO bet_atoms
               (id,plan_id,opportunity_version_id,match_id,source,market_type,line,
                selection_code,odds,stake,rebate_rate,settlement_rule,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"BA_{_uuid.uuid4().hex[:16]}", plan_id, None, p["matches"][0]["match_id"],
             "TC", "PARLAY", pl["combos"], "MULTI", str(pl.get("return_on_hit_min", "")),
             str(pl["stake"]), str(TC_REBATE_RATE), "parlay_v1", ts))
        # 2) 每场受让对冲单关
        for h in p.get("hedges", []):
            db.execute(
                """INSERT INTO bet_atoms
                   (id,plan_id,opportunity_version_id,match_id,source,market_type,line,
                    selection_code,odds,stake,rebate_rate,settlement_rule,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (f"BA_{_uuid.uuid4().hex[:16]}", plan_id, None, h["match_id"],
                 "HG", "ASIAN_HANDICAP", h["line"], h["hedge"],
                 h["odds"], h["stake"], str(DF_REBATE_RATE), "ah_v1", ts))
        # 水位迭代：旧版本 → 新版本
        if old_ticket_id:
            try:
                old_qvs = _json_loads(cur["quote_versions_json"]) if cur["quote_versions_json"] else []
                _write_water_iterations(db, p["matches"][0]["match_id"], old_qvs, qvs,
                                        old_ticket_id, ticket_id)
            except Exception as _e:  # pragma: no cover
                import sys as _sys
                print(f"[warn] water iteration write failed: {_e}", file=_sys.stderr)
    db.close()
    return {"ticket_id": ticket_id, "version": new_version, "cached": False,
            "plan_id": plan_id, "signature": sig}


def _json_dumps(x) -> str:
    import json as _json
    return _json.dumps(x, ensure_ascii=False)


def _json_loads(s: str | None):
    import json as _json
    return _json.loads(s) if s else []


def generate_plans(budget: Decimal = D("1000"), step: Decimal = D("1"),
                   min_stake: Decimal = D("1")) -> dict[str, Any]:
    """从库内跨端比赛生成全部 2串1 / 3串1 方案（两两/三三组合）。"""
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT m.id FROM matches m
        JOIN markets mk ON mk.canonical_match_id=m.id
        JOIN quotes q ON q.market_id=mk.id
        WHERE q.source='DAFABET' AND q.is_valid=1 AND EXISTS (
            SELECT 1 FROM markets mk2 JOIN quotes q2 ON q2.market_id=mk2.id
            WHERE mk2.canonical_match_id=m.id AND q2.source='TC' AND q2.is_valid=1)
        GROUP BY m.id""").fetchall()
    ids = [r["id"] for r in rows]
    result: dict[str, Any] = {"budget": str(budget), "matches_pool": len(ids), "plans": []}
    # 2串1：两两组合
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            p = solve_parlay([ids[i], ids[j]], budget, step, min_stake)
            if p and p.get("status") == "SOLVED":
                p["plan_id"] = f"P2_{ids[i]}_{ids[j]}"
                _attach_ticket(p)
                result["plans"].append(p)
    # 3串1：三三组合
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            for k in range(j + 1, len(ids)):
                p = solve_parlay([ids[i], ids[j], ids[k]], budget, step, min_stake)
                if p and p.get("status") == "SOLVED":
                    p["plan_id"] = f"P3_{ids[i]}_{ids[j]}_{ids[k]}"
                    _attach_ticket(p)
                    result["plans"].append(p)
    result["plan_count"] = len(result["plans"])
    return result


def _attach_ticket(p: dict[str, Any]) -> None:
    """persist_plan → 方案附带 ticket 版本信息（幂等：同签名返回缓存版）。"""
    r = persist_plan(p)
    p["ticket_id"] = r["ticket_id"]
    p["version"] = r["version"]
    p["plan_cached"] = r.get("cached", False)
    p["plan_signature"] = r["signature"]


if __name__ == "__main__":
    import json
    plans = generate_plans()
    print(json.dumps(plans, ensure_ascii=False, indent=1)[:4000])
