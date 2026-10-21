"""P0 实时调度器：后台轮询采集 → 归并 → 跨端计算 → 机会告警。

- TC：HTTP 接口快（0.3s），每 60 秒采集
- DAFABET：浏览器采集慢（~48s），每 300 秒采集（页面不稳自动重试）
- 每次采集完成后自动：归并 → 全部跨端比赛权威计算 → 写入机会报告 + 日志

启动：
    python -m backend.scheduler          # 前台运行（Ctrl+C 停止）
    python -m backend.scheduler --once   # 只跑一轮（调试用）
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.collector.collector import collect  # noqa: E402
from backend.data_process.match_merger import run_merge  # noqa: E402
from backend.engine.engine import authoritative_calculate  # noqa: E402
from backend.db import init_db, seed_capabilities  # noqa: E402

init_db()
seed_capabilities()

LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(LOG_DIR / "scheduler.log"),
    level=logging.INFO,
    encoding="utf-8",
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("scheduler")

DB = ROOT / "data" / "football.db"
REPORT = ROOT / "data" / "live_opportunities.json"


def load_sources() -> list[dict]:
    cfg = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
    return cfg["sources"]


def collect_one(src: dict) -> dict:
    t0 = time.time()
    try:
        r = collect(src)
        return {"name": src["name"], "status": r.get("status"),
                "matches": r.get("matches"), "quotes_changed": r.get("quotes_changed"),
                "elapsed_s": round(time.time() - t0, 1)}
    except Exception as e:
        return {"name": src["name"], "status": "ERROR", "error": str(e)[:200],
                "elapsed_s": round(time.time() - t0, 1)}


def cross_calculate(match_ids: set[str] | None = None) -> list[dict]:
    """跨端比赛权威计算，返回机会列表。
    match_ids=None → 全部两端有报价的比赛；否则只重算指定场次（P0.8 增量重算）。"""
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT m.id, m.home_team, m.away_team FROM matches m
        JOIN markets mk ON mk.canonical_match_id=m.id
        JOIN quotes q ON q.market_id=mk.id
        WHERE (q.source='DAFABET' OR q.source LIKE 'ODDSAPI%') AND q.is_valid=1 AND EXISTS (
            SELECT 1 FROM markets mk2 JOIN quotes q2 ON q2.market_id=mk2.id
            WHERE mk2.canonical_match_id=m.id AND q2.source='TC' AND q2.is_valid=1)
        GROUP BY m.id""").fetchall()
    if match_ids is not None:
        rows = [r for r in rows if r["id"] in match_ids]
    summary = []
    for r in rows:
        try:
            res = authoritative_calculate(r["id"])
        except Exception as e:
            summary.append({"match": f"{r['home_team']} vs {r['away_team']}",
                            "status": "ERROR", "error": str(e)[:120]})
            continue
        item = {"match": f"{r['home_team']} vs {r['away_team']}", "match_id": r["id"],
                "status": res.get("status"), "opportunities": res.get("opportunities") or [],
                "infeasible": len(res.get("infeasible_markets") or [])}
        summary.append(item)
    return summary


def _dirty_match_ids(since_seconds: float = 300.0) -> set[str]:
    """最近 since_seconds 秒内报价/事件发生变化的 canonical_match_id（增量重算候选）。"""
    import time as _t
    cutoff = _t.time() - since_seconds
    db = sqlite3.connect(str(DB))
    ids = {r[0] for r in db.execute(
        """SELECT DISTINCT canonical_match_id FROM quote_events
           WHERE created_at >= ? OR received_at >= ?""",
        (datetime.fromtimestamp(cutoff, timezone.utc).isoformat(),
         datetime.fromtimestamp(cutoff, timezone.utc).isoformat()),
    ).fetchall()}
    db.close()
    return ids


def _update_source_health(name: str, r: dict) -> None:
    """P0.8 源健康：每轮采集后更新 last_success/latency/failure_streak。"""
    now = datetime.now(timezone.utc).isoformat()
    db = sqlite3.connect(str(DB))
    db.row_factory = sqlite3.Row
    row = db.execute("SELECT * FROM source_health WHERE source=?", (name,)).fetchone()
    old_status = (row["last_status"] if row else None)
    if r["status"] == "ERROR":
        streak = (row["failure_streak"] + 1) if row else 1
        status = "WARNING" if streak >= 3 else "ERROR"
        db.execute(
            """INSERT INTO source_health(source,last_success_at,last_failure_at,failure_streak,
               last_observation_at,last_quote_at,collector_latency_ms,last_status,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 last_failure_at=excluded.last_failure_at, failure_streak=excluded.failure_streak,
                 collector_latency_ms=excluded.collector_latency_ms,
                 last_status=excluded.last_status, updated_at=excluded.updated_at""",
            (name, None, now, streak, None, None, r.get("elapsed_s", 0) * 1000, status, now))
    elif r["status"] == "OK":
        db.execute(
            """INSERT INTO source_health(source,last_success_at,last_failure_at,failure_streak,
               last_observation_at,last_quote_at,collector_latency_ms,last_status,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 last_success_at=excluded.last_success_at, failure_streak=0,
                 last_observation_at=excluded.last_observation_at,
                 last_quote_at=excluded.last_quote_at,
                 collector_latency_ms=excluded.collector_latency_ms,
                 last_status=excluded.last_status, updated_at=excluded.updated_at""",
            (name, now, None, 0, now,
             now if r.get("quotes_changed") else None,
             r.get("elapsed_s", 0) * 1000, "HEALTHY", now))
    else:
        # DISABLED / UNAVAILABLE / WARNING 等：如实记录状态，不动 success/failure 字段
        streak = (row["failure_streak"] if row else 0)
        db.execute(
            """INSERT INTO source_health(source,last_success_at,last_failure_at,failure_streak,
               last_observation_at,last_quote_at,collector_latency_ms,last_status,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source) DO UPDATE SET
                 failure_streak=excluded.failure_streak,
                 collector_latency_ms=excluded.collector_latency_ms,
                 last_status=excluded.last_status, updated_at=excluded.updated_at""",
            (name, None, now, streak, None, None, r.get("elapsed_s", 0) * 1000,
             r["status"], now))
    db.commit()
    db.close()
    _alert_health_change(name, old_status, r["status"])
    db.close()


def run_round(reason: str) -> dict:
    """一轮：采集全部启用源 → 归并 → 计算 → 报告。"""
    log.info("== round start (%s) ==", reason)
    sources = load_sources()
    reports = [collect_one(s) for s in sources if s.get("enabled")]
    for r in reports:
        log.info("collect %s: %s matches=%s changed=%s %ss",
                 r["name"], r["status"], r.get("matches"), r.get("quotes_changed"), r.get("elapsed_s"))
    try:
        m = run_merge(verbose=False)
        log.info("merge: %s quotes merged, %d skipped", m["merged_quotes"], len(m["skipped"]))
    except Exception as e:
        log.error("merge failed: %s", e)
    try:
        summary = cross_calculate()
        opps = [s for s in summary if s.get("opportunities")]
        REPORT.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "matches_checked": len(summary),
            "opportunity_count": len(opps),
            "opportunities": opps,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log.info("calc: %d matches checked, %d opportunities ★", len(summary), len(opps))
        if opps:
            for o in opps:
                msg = f"★ 套利机会: {o['match']} " + "; ".join(
                    f"{x.get('classification')} pmin={x.get('min_net_pnl')} stake={x.get('total_stake')}"
                    for x in o["opportunities"][:3])
                log.warning(msg)
                print(msg)
    except Exception as e:
        log.error("calc failed: %s", e)
    log.info("== round end ==")
    return {"collected": reports}


_health_alert_ts: dict[str, tuple[str, float]] = {}  # source -> (status, unix_ts)，节流


def _alert_health_change(name: str, old_status: str | None, new_status: str) -> None:
    """源健康恶化/恢复告警（可选）：仅状态变化时推送；同源同状态 30 分钟内去重。"""
    import time as _time
    if not old_status or old_status == new_status:
        return
    if new_status == "HEALTHY":
        _feishu_alert(f"✅ 源恢复：{name} → {new_status}")
        return
    last = _health_alert_ts.get(name)
    now_ts = _time.time()
    if last and last[0] == new_status and now_ts - last[1] < 1800:
        return
    _health_alert_ts[name] = (new_status, now_ts)
    _feishu_alert(f"⚠️ 源健康恶化：{name} → {new_status}")


def _feishu_alert(text: str) -> None:
    """可选飞书机器人推送（config alert.feishu_webhook 为空则跳过）。"""
    import urllib.request
    try:
        cfg = json.loads((ROOT / "config" / "sources.json").read_text(encoding="utf-8"))
        hook = (cfg.get("alert") or {}).get("feishu_webhook", "")
        if not hook:
            return
        body = json.dumps({"msg_type": "text", "content": {"text": text}},
                          ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(hook, data=body,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8)
        log.info("feishu alert sent: %s", text[:60])
    except Exception as e:
        log.error("feishu alert failed: %s", e)


def _merge_and_calc(reason: str) -> None:
    """归并 + 增量跨端计算（只重算最近变化的场次）+ 写机会报告。"""
    try:
        m = run_merge(verbose=False)
        log.info("merge (%s): %s merged", reason, m["merged_quotes"])
        dirty = _dirty_match_ids(since_seconds=300.0)
        if not dirty:
            log.info("calc (%s): no dirty match, skip", reason)
            return
        summary = cross_calculate(match_ids=dirty)
        opps = [s for s in summary if s.get("opportunities")]
        REPORT.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "matches_checked": len(summary),
            "opportunity_count": len(opps),
            "opportunities": opps,
        }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        log.info("calc (%s): %d/%d dirty matches checked, %d opportunities ★",
                 reason, len(summary), len(dirty), len(opps))
        if opps:
            for o in opps:
                msg = f"★ 套利机会: {o['match']} " + "; ".join(
                    f"{x.get('classification')} pmin={x.get('min_net_pnl')} stake={x.get('total_stake')}"
                    for x in o["opportunities"][:3])
                log.warning(msg)
                print(msg)
            _feishu_alert("★ 足球对冲套利机会\n" + "\n".join(
                f"{o['match']} | Pmin={o['opportunities'][0].get('min_net_pnl')} | 总投入={o['opportunities'][0].get('total_stake')}"
                for o in opps[:3]))
    except Exception as e:
        log.error("merge/calc (%s) failed: %s", reason, e)


def run_loop(intervals: dict[str, float]) -> None:
    """每源独立线程轮询；TC 报价变化即时触发 Dafabet 驻留重读（poke 联动）。"""
    sources = load_sources()
    stop = threading.Event()
    df_poke = threading.Event()  # TC 报价变化 → Dafabet 立即 poll

    def worker(src: dict):
        interval = intervals.get(src["name"], 60.0)
        # DAFABET 驻留模式：浏览器启动一次，秒级读 DOM（页面变化才入库）
        if src.get("resident") and src["name"] == "DAFABET":
            from backend.collector.dafabet_session import DafabetSession
            poll_s = float(src.get("poll_seconds", 25))
            reload_every = int(src.get("reload_every", 10))
            session = None
            try:
                session = DafabetSession(src)
                session.start()
                log.info("dafabet resident session started (poll %ss, reload every %s)",
                         poll_s, reload_every)
            except Exception as e:
                log.error("dafabet resident start failed: %s", e)
                _update_source_health(src["name"],
                                      {"status": "UNAVAILABLE",
                                       "reason": str(e)[:120], "elapsed_s": 0})
            while not stop.is_set():
                if session is None:
                    try:
                        session = DafabetSession(src)
                        session.start()
                        _update_source_health(src["name"], {"status": "OK",
                                                            "quotes_changed": 0, "elapsed_s": 0})
                    except Exception as e:
                        log.error("dafabet resident restart failed: %s", e)
                        time.sleep(30)
                        continue
                try:
                    r = session.poll_once(reload_every=reload_every)
                except Exception as e:
                    log.error("dafabet resident poll error: %s", e)
                    time.sleep(poll_s)
                    continue
                if r.get("changed"):
                    log.info("dafabet resident: %d matches, %d quotes changed",
                             r.get("matches", 0), r.get("quotes_changed", 0))
                    _update_source_health(src["name"],
                                          {"status": "OK", "quotes_changed": r.get("quotes_changed", 0),
                                           "elapsed_s": r.get("elapsed_s", 0)})
                    _merge_and_calc("dafabet-resident")
                else:
                    log.debug("dafabet resident: %s", r.get("reason", "no_change"))
                # 等待间隔；TC 报价变化（poke）时立即醒来重读，不等到下个周期
                if df_poke.wait(poll_s):
                    df_poke.clear()
                    log.info("dafabet resident: TC 报价变化触发即时重读")
            return

        cycle = 0
        while not stop.is_set():
            r = collect_one(src)
            log.info("scheduled collect %s: %s (%ss)", src["name"], r["status"], r.get("elapsed_s"))
            _update_source_health(src["name"], r)
            changed = bool(r.get("quotes_changed"))
            if src["name"] == "TC" and changed:
                df_poke.set()  # 两端时间对齐：TC 动了，Dafabet 立刻读
                log.info("TC quotes changed → poke Dafabet resident")
            _merge_and_calc(f"after-{src['name']}")
            # 定期清理过期场次（开赛后报价失效，只做历史追踪）
            cycle += 1
            if src["name"] == "TC" and cycle % 10 == 0:
                try:
                    from backend.data_process.purge_expired import purge_expired
                    st = purge_expired()
                    if st["matches_finished"] or st["quotes_invalidated"]:
                        log.info("purge expired: %s", st)
                except Exception as e:
                    log.error("purge expired failed: %s", e)
            time.sleep(interval)

    threads = []
    for s in sources:
        if s.get("enabled"):
            t = threading.Thread(target=worker, args=(s,), daemon=True, name=f"poll-{s['name']}")
            t.start()
            threads.append(t)
            log.info("scheduler thread started: %s (interval %ss)", s["name"], intervals.get(s["name"], 60))
    print(f"调度器已启动: {', '.join(f'{s['name']} 每 {intervals.get(s['name'], 60):.0f} 秒' for s in sources if s.get('enabled'))}")
    try:
        while not stop.is_set():
            time.sleep(5)
    except KeyboardInterrupt:
        stop.set()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--tc-interval", type=float, default=15, help="TC 轮询秒数")
    ap.add_argument("--df-interval", type=float, default=60, help="DAFABET 冷启动兜底轮询秒数")
    args = ap.parse_args()
    if args.once:
        run_round("once")
        return
    run_loop({"TC": args.tc_interval, "DAFABET": args.df_interval})


if __name__ == "__main__":
    main()
