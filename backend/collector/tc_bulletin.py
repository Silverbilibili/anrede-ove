"""TC 公告采集（官方赛事公告：销售安排 / 无效场次）

接口: https://webapi.sporttery.cn/gateway/jc/common/gmBulletin.qry?page=1&pageSize=20&isShowHis=1
路径 /gateway/jc/common/ 不受本机 EdgeOne 风控（2026-10-06 实测）。
分类:
  - SALES_SCHEDULE  销售安排（开售/停售时间）
  - INVALID_MATCH   无效场次（退票，影响结算）
  - OTHER           其他
"""
from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.db import db

ROOT = Path(__file__).resolve().parents[2]

BULLETIN_URL = ("https://webapi.sporttery.cn/gateway/jc/common/gmBulletin.qry"
                "?page=1&pageSize=20&isShowHis=1")
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://m.sporttery.cn/ssgg/",
    "Accept": "application/json",
}

_MATCH_NUM_RE = re.compile(r"(周[一二三四五六日]\d{3}|周[一二三四五六日]\s*\d{3})")
_SALE_TIME_RE = re.compile(r"开售时间为(\d{1,2}):(\d{2})[^。]*?停售时间为(\d{1,2}):(\d{2})")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_bulletins(url: str = BULLETIN_URL) -> Any:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def classify(subject: str, content: str) -> tuple[str, str | None, str | None, str | None]:
    """返回 (category, sale_open, sale_close, invalid_match_num)"""
    text = f"{subject} {content}"
    if "销售安排" in subject or "开售时间" in text:
        m = _SALE_TIME_RE.search(text)
        if m:
            open_t = f"{int(m.group(1)):02d}:{m.group(2)}"
            close_t = f"{int(m.group(3)):02d}:{m.group(4)}"
            return "SALES_SCHEDULE", open_t, close_t, None
        return "SALES_SCHEDULE", None, None, None
    if "无效" in text or "退票" in text:
        m = _MATCH_NUM_RE.search(text)
        num = m.group(1).replace(" ", "") if m else None
        return "INVALID_MATCH", None, None, num
    return "OTHER", None, None, None


def upsert_bulletins(raw: Any) -> dict[str, Any]:
    data = (raw.get("value") or {}).get("data") or []
    inserted = 0
    with db() as conn:
        for b in data:
            bid = str(b.get("bulletinId") or "")
            if not bid:
                continue
            subject = str(b.get("bulletinSubject") or "")
            content = str(b.get("bulletinContent") or "")
            category, open_t, close_t, invalid_num = classify(subject, content)
            exists = conn.execute("SELECT bulletin_id FROM bulletins WHERE bulletin_id=?", (bid,)).fetchone()
            if exists:
                conn.execute(
                    """UPDATE bulletins SET
                       subject=?, content=?, category=?,
                       sale_open_time=?, sale_close_time=?, invalid_match_num=?,
                       start_time=?, end_time=?
                       WHERE bulletin_id=?""",
                    (subject, content, category, open_t, close_t, invalid_num,
                     b.get("bulletinStarttime"), b.get("bulletinEndtime"), bid),
                )
                continue
            conn.execute(
                """INSERT INTO bulletins
                   (bulletin_id, subject, content, category,
                    sale_open_time, sale_close_time, invalid_match_num,
                    start_time, end_time, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (bid, subject, content, category, open_t, close_t, invalid_num,
                 b.get("bulletinStarttime"), b.get("bulletinEndtime"), now_iso()),
            )
            inserted += 1
    return {"total": len(data), "inserted": inserted}


def collect_bulletins() -> dict[str, Any]:
    try:
        raw = fetch_bulletins()
        return {"status": "OK", **upsert_bulletins(raw)}
    except Exception as exc:
        return {"status": "FAILED", "error": str(exc)}


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from backend.db import init_db
    init_db()
    result = collect_bulletins()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    with db() as conn:
        rows = conn.execute("SELECT bulletin_id, subject, category, sale_open_time, sale_close_time FROM bulletins ORDER BY start_time DESC LIMIT 5").fetchall()
        for r in rows:
            print(dict(r))
