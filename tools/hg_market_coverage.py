# -*- coding: utf-8 -*-
"""V3-A: HG market coverage report from a REAL captured transform.php response.

Evidence-based audit: counts every IOR_*/RATIO_* market field present per game
node, plus open/closed status. Run:
    python tools/hg_market_coverage.py [xml_path]
Outputs console report + runtime/hg_market_coverage.json.
"""
from __future__ import annotations

import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MARKET_GROUPS = {
    "1X2(独赢)": ("IOR_RMH", "IOR_RMC", "IOR_RMN"),
    "让球(独赢让球)": ("IOR_RGH", "IOR_RGN", "IOR_RGC"),
    "大小球": ("IOR_ROUH", "IOR_ROUHU", "IOR_ROUHO", "IOR_ROUC", "IOR_ROUCO", "IOR_ROUCU"),
    "半场1X2": ("IOR_HRMH", "IOR_HRMC", "IOR_HRMN"),
    "半场大小": ("IOR_HROUH", "IOR_HROUC"),
    "波胆(比分大小)": ("IOR_TXRUO", "IOR_TXRUU", "IOR_XROUO", "IOR_XROUU"),
    "双方进球": ("IOR_RSCY", "IOR_RSCN"),
    "下一进球": ("IOR_RSHY", "IOR_RSHN"),
    "单双进球": ("IOR_REOE", "IOR_REOO"),
    "全场/半场双胜(HT/FT)": ("IOR_REC", "IOR_REH", "IOR_HREC", "IOR_HREH"),
    "其他(比分盘)": ("IOR_RPXN", "IOR_RPXH", "IOR_RPXC", "IOR_RTSY", "IOR_RTSN"),
    "角球相关": ("RATIO_ROUO", "RATIO_ROUU", "RATIO_ROUHO", "RATIO_ROUHU",
                 "RATIO_ROUCO", "RATIO_ROUCU", "RATIO_TXRUO", "RATIO_TXRUU",
                 "RATIO_XROUO", "RATIO_XROUU", "RATIO_RE", "RATIO_HRE",
                 "RATIO_HROUO", "RATIO_HROUU"),
}


def parse_xml_games(text: str) -> list[dict]:
    games = []
    for gm in re.finditer(r"<game\b.*?</game>", text, re.S):
        g = gm.group(0)
        def f(tag: str) -> str:
            m = re.search(r"<%s[^>]*>([^<]*)</%s>" % (tag, tag), g)
            return m.group(1).strip() if m else ""
        games.append({
            "gid": f("GID"), "home": f("TEAM_H"), "away": f("TEAM_C"),
            "league": f("LEAGUE"), "gopen": f("GOPEN"), "hgopen": f("HGOPEN"),
            "running": f("RUNNING"), "kickoff": f("DATETIME"),
            "fields": collections.Counter(re.findall(r"<(IOR_[A-Z0-9_]+|RATIO_[A-Z0-9_]+)\b", g)),
        })
    return games


def main(xml_path: str | None = None) -> None:
    src = Path(xml_path) if xml_path else ROOT / "_hg_game_list_sample.xml"
    if not src.exists():
        print(f"XML not found: {src}")
        sys.exit(1)
    text = src.read_text(encoding="utf-8")
    games = parse_xml_games(text)

    report: dict = {
        "source_file": str(src),
        "snapshot_bytes": len(text),
        "game_nodes": len(games),
        "games": [],
        "market_groups": {},
        "gopen_counts": {"Y": 0, "N": 0},
        "parsed_ok": 0, "parsed_fail": 0,
    }
    for g in games:
        report["gopen_counts"][g["gopen"]] = report["gopen_counts"].get(g["gopen"], 0) + 1
        has_1x2 = all(g["fields"].get(k) for k in MARKET_GROUPS["1X2(独赢)"])
        if has_1x2 and g["gopen"] == "Y":
            report["parsed_ok"] += 1
        else:
            report["parsed_fail"] += 1
        report["games"].append({
            "gid": g["gid"], "home": g["home"], "away": g["away"],
            "league": g["league"], "gopen": g["gopen"], "running": g["running"],
            "kickoff": g["kickoff"],
            "present_markets": {name: [f for f in grp if f in g["fields"]]
                                for name, grp in MARKET_GROUPS.items()
                                if any(f in g["fields"] for f in grp)},
            "field_count": len(g["fields"]),
        })

    for name, fields in MARKET_GROUPS.items():
        present = sum(1 for g in games if any(f in g["fields"] for f in fields))
        report["market_groups"][name] = {"fields": fields, "games_present": present}

    out = ROOT / "runtime" / "hg_market_coverage.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"=== HG 市场覆盖报告（{src.name}）===")
    print(f"快照字节: {len(text)} | game 节点: {len(games)} | GOPEN Y/N: "
          f"{report['gopen_counts'].get('Y', 0)}/{report['gopen_counts'].get('N', 0)}")
    print(f"按现有解析器(1X2完整+GOPEN=Y)可入库: {report['parsed_ok']} | 未通过: {report['parsed_fail']}")
    print("\n各市场在 game 节点中的出现率:")
    for name, v in report["market_groups"].items():
        print(f"  {name}: {v['games_present']}/{len(games)}")
    print(f"\n报告已写入: {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
