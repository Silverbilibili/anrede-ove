# -*- coding: utf-8 -*-
"""过期场次清理：开赛时间已过的 match → FINISHED，报价 is_valid=0（只做历史追踪）。

由 scheduler 定期调用（默认每 ~10 轮），保持库内"未开赛 = 可计算"语义新鲜。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from backend.db import DB_PATH, init_db

_FMT = "%Y-%m-%dT%H:%M:%S%z"


def purge_expired(commit: bool = True) -> dict:
    """标 FINISHED + 报价失效。返回清理统计。"""
    init_db()
    db = sqlite3.connect(DB_PATH)
    now = datetime.now(timezone.utc)
    updated_m, updated_q = 0, 0
    for r in db.execute("SELECT id, kickoff_time_utc FROM matches WHERE status='SCHEDULED'"):
        try:
            kt = datetime.strptime(r[1], _FMT).astimezone(timezone.utc)
        except ValueError:
            continue
        if kt < now:
            db.execute(
                "UPDATE matches SET status='FINISHED', status_updated_at=? WHERE id=?",
                (now.isoformat(), r[0]))
            updated_m += 1
            q = db.execute("""UPDATE quotes SET is_valid=0 WHERE market_id IN
                (SELECT id FROM markets WHERE canonical_match_id=?)""", (r[0],))
            updated_q += q.rowcount
    if commit:
        db.commit()
    db.close()
    return {"matches_finished": updated_m, "quotes_invalidated": updated_q}


if __name__ == "__main__":
    print(purge_expired())
