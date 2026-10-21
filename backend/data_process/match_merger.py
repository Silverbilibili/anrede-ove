"""数据处理层：比赛归并器（P0）
把对手源（DAFABET / ODDSAPI，英文队名）报价归并到 TC（中文队名）比赛下，
使引擎能跨端混算。

原理：market 表通过 market_id 归属比赛；把对手报价的 market_id
改挂到 TC 对应比赛的 MATCH_RESULT market 上，引擎按 TC match_id 查报价
即可同时看到两端（source 字段仍区分 TC/DAFABET/ODDSAPI，不会混淆归属）。

匹配规则：队名经 ALIASES 映射后与 TC 完全一致，且开赛时间差 < 48h
（时间统一按 UTC 归一化比较，兼容 TC +08:00 与 Odds API Z）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "football.db"

# 对手源（英文队名报价，需要归并到 TC 场次）；HG 接入后加入列表
OPPONENT_SOURCES = ["DAFABET", "ODDSAPI"]

# Dafabet 英文队名 → TC 中文规范名（P0 手工维护，随新场次补充）
ALIASES: dict[str, str] = {
    "Croatia": "克罗地亚",
    "Spain": "西班牙",
    "Scotland": "苏格兰",
    "Slovenia": "斯洛文尼",
    "Kazakhstan": "哈萨克",
    "Faroe Islands": "法罗群岛",
    "Moldova": "摩尔多瓦",
    "Slovakia": "斯洛伐克",
    "Estonia": "爱沙尼亚",
    "Iceland": "冰岛",
    "Luxembourg": "卢森堡",
    "Bulgaria": "保加利亚",
    "England": "英格兰",
    "Czech Republic": "捷克",
    "Switzerland": "瑞士",
    "North Macedonia": "北马其顿",
    "Albania": "阿尔巴尼亚",
    "San Marino": "圣马力诺",
    "Belarus": "白俄罗斯",
    "Finland": "芬兰",
    "South Korea": "韩国",
    "Uzbekistan": "乌兹别克",
    "China": "中国",
    "Tajikistan": "塔吉克",
    "Chicago Fire": "芝加哥",
    "Vancouver Whitecaps FC": "温哥华",
    "USA": "美国",
    "Canada": "加拿大",
    "Cyprus": "塞浦路斯",
    "Latvia": "拉脱维亚",
    "France": "法国",
    "Belgium": "比利时",
    "Romania": "罗马尼亚",
    "Sweden": "瑞典",
    "Italy": "意大利",
    "Turkey": "土耳其",
    "Northern Ireland": "北爱尔兰",
    "Georgia": "格鲁吉亚",
    "Montenegro": "黑山",
    "Armenia": "亚美尼亚",
    "Bosnia and Herzegovina": "波黑",
    "Poland": "波兰",
    "Ireland": "爱尔兰",
    "Greece": "希腊",
    "Hungary": "匈牙利",
    "Germany": "德国",
    "Netherlands": "荷兰",
    "Portugal": "葡萄牙",
    "Denmark": "丹麦",
    "Norway": "挪威",
    "Ukraine": "乌克兰",
    "Austria": "奥地利",
    "Serbia": "塞尔维亚",
    "Czechia": "捷克",
    "Wales": "威尔士",
    "Australia": "澳大利亚",
    "Japan": "日本",
    "Brazil": "巴西",
    "Argentina": "阿根廷",
}


def alias(name: str) -> str:
    n = name.strip()
    if n in ALIASES:
        return ALIASES[n]
    # 反向（TC 中文 → 英文）不处理；未命中原样返回并提示
    return n


def _to_utc(s: str) -> datetime | None:
    """ISO 时间统一转 UTC（兼容 TC 的 +08:00 与 Odds API 的 Z）。"""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


# 四分一网格（五态结算）：-0.25 / -0.5 / -0.75 / 0.25 ……（含整数盘）
# 兼容 Dafabet 逗号拆分线（"0,-0.5"）与斜杠拆分线（"0/-0.5"）
_QUARTER_LINE = re.compile(r"^[+-]?\d+(\.25|\.5|\.75)?([,/][+-]?\d+(\.25|\.5|\.75)?)?$")


def run_merge(verbose: bool = True) -> dict:
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    merged = 0
    skipped = []
    # 对手源列表 = DAFABET + 全部 ODDSAPI 公司变体（ODDSAPI:Pinnacle / ODDSAPI:Bet365 …）
    oddsapi_sources = [r[0] for r in db.execute(
        "SELECT DISTINCT source FROM quotes WHERE source LIKE 'ODDSAPI%'").fetchall()]
    for src in ["DAFABET"] + oddsapi_sources:
        # 1. 找该源全部比赛（英文队名）
        df_matches = db.execute(
            """SELECT DISTINCT m.id, m.home_team, m.away_team, m.kickoff_time_utc
            FROM matches m JOIN markets mk ON mk.canonical_match_id=m.id
            JOIN quotes q ON q.market_id=mk.id WHERE q.source=?""",
            (src,)).fetchall()
        for dm in df_matches:
            h_cn, a_cn = alias(dm["home_team"]), alias(dm["away_team"])
            if h_cn == dm["home_team"] or a_cn == dm["away_team"]:
                skipped.append(f"[{src}] {dm['home_team']} vs {dm['away_team']} (无映射)")
                continue
            # 2. 在 TC matches 里找同队名比赛（时间差 < 48h，UTC 归一化）
            t0 = _to_utc(dm["kickoff_time_utc"])
            tc_candidates = db.execute(
                "SELECT id, kickoff_time_utc FROM matches WHERE home_team=? AND away_team=?",
                (h_cn, a_cn)).fetchall()
            hit = None
            for tc in tc_candidates:
                t1 = _to_utc(tc["kickoff_time_utc"])
                if t0 is not None and t1 is not None and abs((t1 - t0).total_seconds()) < 48 * 3600:
                    hit = tc
                    break
            if not hit:
                skipped.append(f"[{src}] {h_cn} vs {a_cn} (TC 无同场)")
                continue
            # 3. 找 TC 的 MATCH_RESULT market 与该源 market
            tc_mkt = db.execute(
                "SELECT id FROM markets WHERE canonical_match_id=? AND market_type='MATCH_RESULT'",
                (hit["id"],)).fetchone()
            df_mkts = db.execute(
                "SELECT id FROM markets WHERE canonical_match_id=? AND market_type='MATCH_RESULT'",
                (dm["id"],)).fetchall()
            if not tc_mkt or not df_mkts:
                skipped.append(f"[{src}] {h_cn} vs {a_cn} (缺 market)")
                continue
            # 幂等搬迁（天然幂等）：quotes 的 market_id 改挂后不再属于该源场次，
            # 重复归并不会重复搬迁；不得"先失效再搬"（会导致已搬迁报价丢失）。
            for dfm in df_mkts:
                cur = db.execute(
                    "UPDATE quotes SET market_id=?, canonical_match_id=? WHERE market_id=? AND is_valid=1",
                    (tc_mkt["id"], hit["id"], dfm["id"]))
                merged += cur.rowcount
        # 4. 亚洲让球（五态结算）归并：该源 AH market → TC match（单向，TC 无 AH 盘）
            df_ah_mkts = db.execute(
                "SELECT id, line FROM markets WHERE canonical_match_id=? AND market_type='ASIAN_HANDICAP'",
                (dm["id"],)).fetchall()
            for dfm in df_ah_mkts:
                # 该源拆分线可能带空格（"0, -0.5"），先去空白再匹配四分一网格
                line = re.sub(r"\s+", "", dfm["line"] or "")
                if not _QUARTER_LINE.match(line):
                    continue  # 非四分一网格（异常盘），跳过
                db.execute("UPDATE markets SET canonical_match_id=? WHERE id=?", (hit["id"], dfm["id"]))
                cur = db.execute(
                    "UPDATE quotes SET canonical_match_id=? WHERE market_id=? AND is_valid=1",
                    (hit["id"], dfm["id"]))
                merged += cur.rowcount
            # 4b. 大小球（P1）归并：该源 OVER_UNDER market → TC match（单向）
            df_ou_mkts = db.execute(
                "SELECT id FROM markets WHERE canonical_match_id=? AND market_type='OVER_UNDER'",
                (dm["id"],)).fetchall()
            for dfm in df_ou_mkts:
                db.execute("UPDATE markets SET canonical_match_id=? WHERE id=?", (hit["id"], dfm["id"]))
                cur = db.execute(
                    "UPDATE quotes SET canonical_match_id=? WHERE market_id=? AND is_valid=1",
                    (hit["id"], dfm["id"]))
                merged += cur.rowcount
            if verbose:
                print(f"  {dm['home_team']} vs {dm['away_team']} → {h_cn} vs {a_cn} "
                      f"(TC {hit['id'][:12]}, 报价 {sum(len(db.execute('SELECT 1 FROM quotes WHERE market_id=?', (dfm['id'],)).fetchall()) for dfm in df_mkts)} 条)")
    db.commit()
    return {"merged_quotes": merged, "skipped": skipped}


if __name__ == "__main__":
    print("=== 归并 DAFABET → TC ===")
    r = run_merge()
    print(f"归并报价 {r['merged_quotes']} 条")
    for s in r["skipped"]:
        print(f"  跳过: {s}")
