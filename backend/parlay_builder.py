"""通用 N串1 过关引擎 + 混合过关（V4.1）

支持：
  - N串1: C(N,K) 组合生成，K ∈ [1..N]
  - 混合过关方式：多K值同时选取（如 2串1 + 3串1）
  - 系统串法：3串4(=3×2串1 + 1×3串1), 4串11(=6×2串1 + 4×3串1 + 1×4串1)
  - 实时赔率校验、数据新鲜度验证
  - 金额模式：每注固定金额 / 按投注单分配总金额
  - 组合赔率 = Π(各腿赔率) → 理论返还 = 组合赔率 × 单注金额
  - 总注数 = Σ(各过关方式 C(N,K))，总金额 = 总注数 × 单注金额

原则：
  - 复用 plan_generator.py 的 _sporttery_cent_round, _latest_tc_odds 等基础函数
  - 不从零重建计算体系；兼容现有 plan_generator 输出
  - 所有金额使用 Decimal，禁止 float
"""

from __future__ import annotations

import itertools
import math
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "football.db"

D = Decimal

# ---------- 过关方式定义 ----------
# 每种过关方式: {name, k: 每组合腿数}
PARLAY_TYPES: dict[str, dict[str, Any]] = {
    "SINGLE":   {"name": "单场",      "k": 1, "desc": "单关投注"},
    "PARLAY2":  {"name": "2串1",      "k": 2, "desc": "两场组合过关"},
    "PARLAY3":  {"name": "3串1",      "k": 3, "desc": "三场组合过关"},
    "PARLAY4":  {"name": "4串1",      "k": 4, "desc": "四场组合过关"},
    "PARLAY5":  {"name": "5串1",      "k": 5, "desc": "五场组合过关"},
    "PARLAY6":  {"name": "6串1",      "k": 6, "desc": "六场组合过关"},
    "PARLAY7":  {"name": "7串1",      "k": 7, "desc": "七场组合过关"},
    "PARLAY8":  {"name": "8串1",      "k": 8, "desc": "八场组合过关"},
}

# 系统串法定义：{name: {sub_type: count}}
SYSTEM_PARLAYS: dict[str, dict[str, int]] = {
    "SYS_3_4":  {"name": "3串4",  "desc": "3场赛事，3个2串1 + 1个3串1", "components": {"PARLAY2": 3, "PARLAY3": 1}},
    "SYS_4_11": {"name": "4串11", "desc": "4场赛事，6个2串1 + 4个3串1 + 1个4串1", "components": {"PARLAY2": 6, "PARLAY3": 4, "PARLAY4": 1}},
    "SYS_5_26": {"name": "5串26", "desc": "5场赛事，10个2串1 + 10个3串1 + 5个4串1 + 1个5串1", "components": {"PARLAY2": 10, "PARLAY3": 10, "PARLAY4": 5, "PARLAY5": 1}},
}


# ---------- 数据结构 ----------
@dataclass
class SelectionItem:
    """单个投注项。"""
    selection_id: str          # 唯一标识
    match_id: str
    match_name: str            # "主队 vs 客队"
    competition: str
    kickoff_time: str          # ISO格式
    market_type: str           # "1X2" | "AH" | "OU" | ...
    market_line: str | None    # 盘口参数（AH/OU）
    selection_code: str        # "HOME" | "DRAW" | "AWAY" | ...
    selection_name: str        # "主胜" | "平局" | "客胜" | ...
    odds: Decimal
    source: str                # "TC" | "HG" | "DAFABET" | ...
    quote_version: int
    source_timestamp: str | None
    is_valid: bool = True
    data_freshness: str = "LIVE"  # LIVE | STALE | UNKNOWN
    freshness_note: str = ""


@dataclass
class ParlayCombo:
    """单个过关组合。"""
    combo_id: str
    parlay_type: str           # "PARLAY2" | "PARLAY3" | ...
    k: int                     # 腿数
    selections: list[SelectionItem]
    combo_odds: Decimal        # Π(各腿赔率)
    unit_stake: Decimal        # 单注金额
    theoretical_return: Decimal # 理论返还（未含返点）
    is_valid: bool = True
    invalid_reason: str = ""


@dataclass
class BetSlip:
    """投注单完整状态。"""
    slip_id: str
    selections: list[SelectionItem]       # 已选投注项（去重后）
    parlay_types_selected: list[str]      # 选中的过关方式代码
    system_parlay: str | None             # 系统串法代码（如 SYS_3_4），与 parlay_types 互斥
    combos: list[ParlayCombo]             # 生成的全部组合
    stake_mode: str                       # "per_unit" | "total_distribution"
    unit_stake: Decimal                   # 每注金额
    total_stake: Decimal                  # 总投注金额
    total_combos: int                     # 总组合数
    max_return: Decimal                   # 理论最高返还
    min_return: Decimal                   # 理论最低返还（=0 若未全中）
    status: str                           # "EDITING" | "PENDING_CONFIRM" | "CONFIRMED" | "SUBMITTED"
    created_at: str
    updated_at: str
    notes: list[str]                      # 警告/提示


# ---------- 组合生成 ----------
def _combo_count(n: int, k: int) -> int:
    """C(n,k) = n! / (k! * (n-k)!)"""
    if k > n or k < 1:
        return 0
    return math.comb(n, k)


def generate_n_parlay_combos(selections: list[SelectionItem], k: int,
                              unit_stake: Decimal = D("2")) -> list[ParlayCombo]:
    """从 selections 中选 k 个，生成全部 C(N,K) 个组合。

    Args:
        selections: 有效投注项列表（已去重、同一赛事不重复）
        k: 每组合腿数
        unit_stake: 每注金额（默认2元，竞彩标准）

    Returns:
        ParlayCombo 列表，每个代表一个 K串1 组合
    """
    n = len(selections)
    if k > n:
        return []

    combos: list[ParlayCombo] = []
    parlay_type = f"PARLAY{k}" if k <= 8 else f"PARLAY{k}"

    for idx, combo_sels in enumerate(itertools.combinations(selections, k)):
        sel_list = list(combo_sels)

        # 校验：同一赛事不能出现在同一组合中
        match_ids = [s.match_id for s in sel_list]
        if len(set(match_ids)) < k:
            continue  # 同场多次选择，跳过

        # 校验：所有投注项有效
        invalid = [s for s in sel_list if not s.is_valid]
        combo_odds = D("1")
        for s in sel_list:
            if s.is_valid:
                combo_odds *= s.odds

        combo = ParlayCombo(
            combo_id=f"CB_{uuid.uuid4().hex[:12]}",
            parlay_type=parlay_type,
            k=k,
            selections=sel_list,
            combo_odds=combo_odds,
            unit_stake=unit_stake,
            theoretical_return=combo_odds * unit_stake,
            is_valid=len(invalid) == 0,
            invalid_reason=f"{len(invalid)} invalid selections" if invalid else "",
        )
        combos.append(combo)

    return combos


def generate_mixed_parlay_combos(selections: list[SelectionItem],
                                  parlay_types: list[str],
                                  unit_stake: Decimal = D("2")) -> list[ParlayCombo]:
    """混合过关方式：对每种选定 K 值分别生成组合。

    Args:
        selections: 有效投注项
        parlay_types: 过关方式列表，如 ["PARLAY2", "PARLAY3"]
        unit_stake: 每注金额

    Returns:
        全部组合（按 parlay_type 排序）
    """
    all_combos: list[ParlayCombo] = []
    for pt in parlay_types:
        info = PARLAY_TYPES.get(pt)
        if not info:
            continue
        k = info["k"]
        combos = generate_n_parlay_combos(selections, k, unit_stake)
        all_combos.extend(combos)
    return all_combos


def generate_system_parlay_combos(selections: list[SelectionItem],
                                   system_code: str,
                                   unit_stake: Decimal = D("2")) -> list[ParlayCombo]:
    """系统串法：按定义生成多种过关方式的全部组合。

    例：SYS_3_4 → 3个2串1 + 1个3串1（从3场中选择）
    """
    sys_def = SYSTEM_PARLAYS.get(system_code)
    if not sys_def:
        return []

    all_combos: list[ParlayCombo] = []
    components = sys_def["components"]
    for pt, count in components.items():
        info = PARLAY_TYPES.get(pt)
        if not info:
            continue
        k = info["k"]
        combos = generate_n_parlay_combos(selections, k, unit_stake)
        all_combos.extend(combos)

    return all_combos


# ---------- 投注单构建 ----------
def build_bet_slip(selections: list[SelectionItem],
                   parlay_types: list[str] | None = None,
                   system_code: str | None = None,
                   stake_mode: str = "per_unit",
                   unit_stake: Decimal = D("2"),
                   total_budget: Decimal | None = None) -> BetSlip:
    """从投注项和过关方式构建完整投注单。

    Args:
        selections: 用户选择的投注项列表
        parlay_types: 过关方式如 ["PARLAY2", "PARLAY3"]
        system_code: 系统串法代码，如 "SYS_3_4"（与 parlay_types 互斥）
        stake_mode: "per_unit"（每注固定）= 默认竞彩2元/注 | "total_distribution"（总金额分配）
        unit_stake: 每注金额（stake_mode="per_unit"时使用）
        total_budget: 总预算（stake_mode="total_distribution"时使用）

    Returns:
        BetSlip 完整投注单
    """
    now = datetime.now(timezone.utc).isoformat()
    notes: list[str] = []

    # 去重：相同 match_id + selection_code 只保留一个（保留最新赔率）
    deduped: dict[str, SelectionItem] = {}
    for s in selections:
        key = f"{s.match_id}:{s.market_type}:{s.selection_code}:{s.market_line or ''}"
        if key in deduped:
            # 保留更新鲜的数据
            existing = deduped[key]
            if s.quote_version > existing.quote_version:
                deduped[key] = s
            continue
        deduped[key] = s

    unique_sels = list(deduped.values())

    # 校验：每个赛事只能选一个方向（同玩法下）
    match_seen: dict[str, set[str]] = {}
    valid_sels: list[SelectionItem] = []
    rejected: list[str] = []
    for s in unique_sels:
        ms = match_seen.setdefault(s.match_id, set())
        mk_key = f"{s.market_type}:{s.market_line or ''}"
        if mk_key in ms:
            rejected.append(f"{s.match_name} 同玩法已选，跳过 {s.selection_name}")
            continue
        ms.add(mk_key)
        if not s.is_valid:
            rejected.append(f"{s.match_name} {s.selection_name} 赔率失效")
            continue
        valid_sels.append(s)

    for r in rejected:
        notes.append(r)

    # 生成组合
    combos: list[ParlayCombo] = []
    if system_code and system_code in SYSTEM_PARLAYS:
        combos = generate_system_parlay_combos(valid_sels, system_code, unit_stake)
        notes.append(f"系统串法: {SYSTEM_PARLAYS[system_code]['name']} - {SYSTEM_PARLAYS[system_code]['desc']}")
    elif parlay_types:
        combos = generate_mixed_parlay_combos(valid_sels, parlay_types, unit_stake)
    else:
        # 默认：所有可行组合（2串1 到 N串1）
        n = len(valid_sels)
        default_types = [f"PARLAY{k}" for k in range(2, min(n + 1, 9))]
        combos = generate_mixed_parlay_combos(valid_sels, default_types, unit_stake)

    total_combos = len(combos)

    # 金额计算
    if stake_mode == "total_distribution" and total_budget and total_combos > 0:
        unit_stake = (total_budget / D(str(total_combos))).quantize(D("0.01"), rounding=ROUND_DOWN)
        if unit_stake < D("2"):
            unit_stake = D("2")
            notes.append(f"总预算不足，每注最低2元，实际需 ¥{2 * total_combos}")
    elif stake_mode == "per_unit":
        # 保持用户设置的 unit_stake
        pass

    total_stake = unit_stake * D(str(total_combos))

    # 更新每个组合的单注金额
    for c in combos:
        c.unit_stake = unit_stake
        c.theoretical_return = c.combo_odds * unit_stake

    # 计算理论返还范围
    valid_combos = [c for c in combos if c.is_valid]
    if valid_combos:
        max_return = max(c.theoretical_return for c in valid_combos)
        min_return = min(c.theoretical_return for c in valid_combos)
    else:
        max_return = D("0")
        min_return = D("0")

    slip = BetSlip(
        slip_id=f"SLIP_{uuid.uuid4().hex[:16]}",
        selections=valid_sels,
        parlay_types_selected=parlay_types or [],
        system_parlay=system_code,
        combos=combos,
        stake_mode=stake_mode,
        unit_stake=unit_stake,
        total_stake=total_stake,
        total_combos=total_combos,
        max_return=max_return,
        min_return=min_return,
        status="EDITING",
        created_at=now,
        updated_at=now,
        notes=notes,
    )

    return slip


# ---------- 数据新鲜度验证 ----------
def validate_selection_freshness(sel: SelectionItem,
                                  max_age_seconds: int = 300) -> tuple[bool, str]:
    """检查投注项数据是否仍新鲜。

    Returns:
        (is_fresh, note)
    """
    if sel.data_freshness == "STALE":
        return False, f"{sel.match_name} {sel.selection_name} 数据过期"
    if sel.data_freshness == "UNKNOWN":
        return True, f"{sel.match_name} {sel.selection_name} 数据新鲜度未知（显示为参考赔率）"
    if sel.source_timestamp:
        try:
            ts = datetime.fromisoformat(sel.source_timestamp.replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
            if age > max_age_seconds:
                return False, f"{sel.match_name} {sel.selection_name} 赔率已 {age:.0f}s 未更新"
        except (ValueError, TypeError):
            pass
    return True, ""


def refresh_slip_validity(slip: BetSlip, max_age_seconds: int = 300) -> BetSlip:
    """重新验证投注单中所有投注项的新鲜度，标记失效组合。"""
    notes: list[str] = list(slip.notes)
    changed = False

    for sel in slip.selections:
        fresh, note = validate_selection_freshness(sel, max_age_seconds)
        if not fresh:
            sel.is_valid = False
            sel.data_freshness = "STALE"
            sel.freshness_note = note
            notes.append(note)
            changed = True

    # 重新生成组合（排除失效项）
    if changed:
        valid_sels = [s for s in slip.selections if s.is_valid]
        if slip.system_parlay:
            new_combos = generate_system_parlay_combos(valid_sels, slip.system_parlay, slip.unit_stake)
        elif slip.parlay_types_selected:
            new_combos = generate_mixed_parlay_combos(valid_sels, slip.parlay_types_selected, slip.unit_stake)
        else:
            n = len(valid_sels)
            default_types = [f"PARLAY{k}" for k in range(2, min(n + 1, 9))]
            new_combos = generate_mixed_parlay_combos(valid_sels, default_types, slip.unit_stake)

        slip.combos = new_combos
        slip.total_combos = len(new_combos)
        slip.total_stake = slip.unit_stake * D(str(slip.total_combos))
        valid_combos = [c for c in new_combos if c.is_valid]
        slip.max_return = max((c.theoretical_return for c in valid_combos), default=D("0"))
        slip.min_return = min((c.theoretical_return for c in valid_combos), default=D("0"))
        slip.notes = notes
        slip.updated_at = datetime.now(timezone.utc).isoformat()

    return slip


# ---------- 从数据库读取真实投注项 ----------
def load_selections_from_db(match_ids: list[str] | None = None,
                             market_types: list[str] | None = None,
                             sources: list[str] | None = None,
                             limit: int = 100,
                             window_limit: int = 500) -> list[SelectionItem]:
    """从数据库加载可用投注项（用于构建投注单）。

    Args:
        match_ids: 限制赛事范围（None = 全部未开赛）
        market_types: 限制玩法（None = 全部）
        sources: 限制数据源（None = TC优先）
        limit: 最大返回数
    """
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row

    conditions = ["m.status = 'SCHEDULED'", "q.is_valid = 1"]
    params: list[Any] = []

    if match_ids:
        placeholders = ",".join("?" * len(match_ids))
        conditions.append(f"m.id IN ({placeholders})")
        params.extend(match_ids)

    if market_types:
        placeholders = ",".join("?" * len(market_types))
        conditions.append(f"mk.market_type IN ({placeholders})")
        params.extend(market_types)

    if sources:
        placeholders = ",".join("?" * len(sources))
        conditions.append(f"q.source IN ({placeholders})")
        params.extend(sources)

    where = " AND ".join(conditions)

    query = f"""
        SELECT m.id AS match_id, m.home_team, m.away_team, m.competition,
               m.kickoff_time_utc, mk.market_type, mk.line AS market_line,
               s.code AS selection_code, s.display_name AS selection_name,
               q.odds_decimal, q.source, q.quote_version, q.source_timestamp,
               q.source_status
        FROM matches m
        JOIN markets mk ON mk.canonical_match_id = m.id
        JOIN selections s ON s.market_id = mk.id
        JOIN quotes q ON q.selection_id = s.id AND q.canonical_match_id = m.id
        WHERE {where}
        ORDER BY q.quote_version DESC
        LIMIT {window_limit}
    """

    rows = db.execute(query, params).fetchall()
    db.close()

    seen: set[str] = set()
    items: list[SelectionItem] = []
    for r in rows:
        key = f"{r['match_id']}:{r['market_type']}:{r['selection_code']}:{r['market_line'] or ''}:{r['source']}"
        if key in seen:
            continue
        seen.add(key)

        freshness = "LIVE"
        freshness_note = ""
        if r["source_status"] and r["source_status"] not in ("OPEN",):
            freshness = "STALE"
            freshness_note = f"源状态: {r['source_status']}"

        if len(items) >= limit:
            break
        items.append(SelectionItem(
            selection_id=f"SEL_{uuid.uuid4().hex[:12]}",
            match_id=r["match_id"],
            match_name=f"{r['home_team']} vs {r['away_team']}",
            competition=r["competition"] or "",
            kickoff_time=r["kickoff_time_utc"] or "",
            market_type=r["market_type"],
            market_line=r["market_line"],
            selection_code=r["selection_code"],
            selection_name=r["selection_name"] or r["selection_code"],
            odds=D(r["odds_decimal"]),
            source=r["source"],
            quote_version=r["quote_version"] or 0,
            source_timestamp=r["source_timestamp"],
            is_valid=(r["source_status"] is None or r["source_status"] == "OPEN"),
            data_freshness=freshness,
            freshness_note=freshness_note,
        ))

    return items[:limit]


# ---------- 投注单序列化（API 返回） ----------
def slip_to_dict(slip: BetSlip) -> dict[str, Any]:
    """将 BetSlip 序列化为 API 可返回的 dict。"""
    return {
        "slip_id": slip.slip_id,
        "status": slip.status,
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
                "freshness_note": s.freshness_note,
            }
            for s in slip.selections
        ],
        "parlay_types_selected": slip.parlay_types_selected,
        "system_parlay": slip.system_parlay,
        "stake_mode": slip.stake_mode,
        "unit_stake": str(slip.unit_stake),
        "total_stake": str(slip.total_stake),
        "total_combos": slip.total_combos,
        "max_return": str(slip.max_return),
        "min_return": str(slip.min_return),
        "combos": [
            {
                "combo_id": c.combo_id,
                "parlay_type": c.parlay_type,
                "k": c.k,
                "selections": [s.selection_id for s in c.selections],
                "combo_odds": str(c.combo_odds),
                "unit_stake": str(c.unit_stake),
                "theoretical_return": str(c.theoretical_return),
                "is_valid": c.is_valid,
                "invalid_reason": c.invalid_reason,
                "summary": " × ".join(
                    f"{s.match_name[:20]} {s.selection_name} @{s.odds}"
                    for s in c.selections
                ),
            }
            for c in slip.combos
        ],
        "breakdown": _breakdown_by_type(slip),
        "notes": slip.notes,
        "created_at": slip.created_at,
        "updated_at": slip.updated_at,
        "data_summary": {
            "total_selections": len(slip.selections),
            "valid_selections": sum(1 for s in slip.selections if s.is_valid),
            "stale_selections": sum(1 for s in slip.selections if s.data_freshness == "STALE"),
            "sources": list(set(s.source for s in slip.selections)),
        },
    }


def _breakdown_by_type(slip: BetSlip) -> list[dict[str, Any]]:
    """按过关方式分类统计。"""
    by_type: dict[str, dict[str, Any]] = {}
    for c in slip.combos:
        pt = c.parlay_type
        if pt not in by_type:
            by_type[pt] = {"parlay_type": pt, "name": PARLAY_TYPES.get(pt, {}).get("name", pt),
                           "k": c.k, "combo_count": 0, "total_stake": D("0"),
                           "max_return": D("0"), "combos": []}
        by_type[pt]["combo_count"] += 1
        by_type[pt]["total_stake"] += c.unit_stake
        if c.theoretical_return > by_type[pt]["max_return"]:
            by_type[pt]["max_return"] = c.theoretical_return

    result = []
    for pt, info in sorted(by_type.items()):
        result.append({
            "parlay_type": info["parlay_type"],
            "name": info["name"],
            "k": info["k"],
            "combo_count": info["combo_count"],
            "total_stake": str(info["total_stake"]),
            "max_return": str(info["max_return"]),
        })
    return result


# ---------- 可用过关方式推荐 ----------
def available_parlay_types(selection_count: int) -> list[dict[str, Any]]:
    """根据已选投注项数量，返回可用的过关方式列表。"""
    available = []
    for pt, info in PARLAY_TYPES.items():
        if pt == "SINGLE":
            available.append({**info, "type": pt, "available": True, "combo_count": selection_count})
        else:
            k = info["k"]
            if k <= selection_count:
                cnt = _combo_count(selection_count, k)
                available.append({**info, "type": pt, "available": True,
                                  "combo_count": cnt, "k": k})
            else:
                available.append({**info, "type": pt, "available": False,
                                  "reason": f"需要至少{k}个投注项（当前{selection_count}）"})

    for sc, sinfo in SYSTEM_PARLAYS.items():
        # System parlays require exact N (e.g., 3串4 needs exactly 3 selections)
        n_needed = max(PARLAY_TYPES[pt]["k"] for pt in sinfo["components"])
        is_available = selection_count == n_needed
        total_combos = sum(
            _combo_count(selection_count, PARLAY_TYPES[pt]["k"])
            for pt in sinfo["components"]
        ) if is_available else 0
        reason = "" if is_available else (
            f"系统串法需恰好{n_needed}个投注项（当前{selection_count}）"
            if selection_count >= n_needed else
            f"需要至少{n_needed}个投注项（当前{selection_count}）"
        )
        available.append({
            "type": sc, "name": sinfo["name"], "desc": sinfo["desc"],
            "available": is_available,
            "combo_count": total_combos,
            "reason": reason,
        })

    return available

