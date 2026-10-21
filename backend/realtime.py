"""实时采集 + SSE 推送（端到端 1s 级）。

架构:
  FastPollWorker（FastAPI 生命周期内常驻）
    · 每 1s 轻量请求竞彩官方赛程接口（仅内存对比，不写库）
    · 检测到真实赔率变化才调用 collect() 完整入库，并通过 EventBus 广播
    · 请求异常/被风控时自适应退避（指数翻倍，恢复后还原）
  EventBus / SSE 端点 /api/v1/stream
    · 前端建立 EventSource 后，官方变盘即被即时推送，无需轮询
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import Request
from fastapi.responses import StreamingResponse

from backend.collector.collector import _tc_fb_fetch_schedule, process_tc_fb_schedule
from backend.collector.tc_bulletin import collect_bulletins
from backend.hg_direct import HGDirectWorker
from backend.hg_http import HGHttpCollector


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EventBus:
    """SSE 连接管理与广播。"""

    def __init__(self) -> None:
        self._clients: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called from the asyncio startup so background threads can broadcast."""
        self._loop = loop

    async def register(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._clients.add(q)
        return q

    def unregister(self, q: asyncio.Queue) -> None:
        self._clients.discard(q)

    async def broadcast(self, event: dict) -> None:
        data = json.dumps(event, ensure_ascii=False, default=str)
        full: list[asyncio.Queue] = []
        for q in tuple(self._clients):
            try:
                q.put_nowait(data)
            except asyncio.QueueFull:
                full.append(q)
        for q in full:
            self._clients.discard(q)

    def broadcast_threadsafe(self, event: dict) -> None:
        """Schedule broadcast() from a non-asyncio thread (e.g. HGDirectWorker)."""
        if self._loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self.broadcast(event), self._loop)
        except Exception:
            pass

    @property
    def client_count(self) -> int:
        return len(self._clients)


bus = EventBus()


def _extract_sig(raw: Any) -> dict[str, dict]:
    """从赛程 JSON 提取 {match_id: {h,d,a,status}}。"""
    out: dict[str, dict] = {}
    for g in (raw.get("value") or {}).get("matchInfoList") or []:
        for m in g.get("subMatchList", []):
            out[str(m.get("matchId"))] = {
                "h": m.get("h"), "d": m.get("d"), "a": m.get("a"),
                "st": str(m.get("matchStatus")),
            }
    return out


def _build_live_feed(raw: Any, observed_at: str | None = None) -> dict[str, Any]:
    """把 TC 高频轮询快照直接转换成前端可消费的内存 feed。

    这一步不访问 SQLite，避免“源已变 → SSE → 浏览器再 fetch DB”这条额外链路。
    数据仍然只来自 TC 官方公开 football feed。
    """
    from decimal import Decimal

    observed_at = observed_at or _now()
    matches: list[dict[str, Any]] = []
    groups = (raw.get("value") or {}).get("matchInfoList") or []
    for g in groups:
        for m in g.get("subMatchList", []):
            if str(m.get("matchStatus") or "") != "2":
                continue
            h, d, a = str(m.get("h") or ""), str(m.get("d") or ""), str(m.get("a") or "")
            if not h or not d or not a:
                continue
            try:
                odds = {"HOME": Decimal(h), "DRAW": Decimal(d), "AWAY": Decimal(a)}
                inv_sum = sum(Decimal(1) / v for v in odds.values())
                return_rate = Decimal(1) / inv_sum
            except Exception:
                continue
            biz_date = m.get("businessDate") or g.get("businessDate") or g.get("matchDate", "")
            mt = str(m.get("matchTime") or "00:00:00")
            if len(mt) == 5:
                mt += ":00"
            kickoff = f"{biz_date}T{mt}+08:00"
            matches.append({
                "match_id": str(m.get("matchId") or ""),
                "competition": m.get("leagueAbbName") or m.get("leagueAllName") or "",
                "home": m.get("homeTeamAbbName") or m.get("homeTeamAllName") or "",
                "away": m.get("awayTeamAbbName") or m.get("awayTeamAllName") or "",
                "kickoff_time_utc": kickoff,
                "quotes": {
                    "HOME": {"odds": h, "quote_version": 0, "source_status": "OPEN", "source_timestamp": observed_at},
                    "DRAW": {"odds": d, "quote_version": 0, "source_status": "OPEN", "source_timestamp": observed_at},
                    "AWAY": {"odds": a, "quote_version": 0, "source_status": "OPEN", "source_timestamp": observed_at},
                },
                "market_version": 0,
                "tc_return_rate_pct": str((return_rate * 100).quantize(Decimal("0.01"))),
                "tc_margin_pct": str(((inv_sum - 1) * 100).quantize(Decimal("0.01"))),
                "updated_at": observed_at,
                "source_url": "https://m.sporttery.cn/mjc/zqsj/?tab=concern",
            })
    matches.sort(key=lambda x: (x["kickoff_time_utc"], x["match_id"]))
    return {
        "data_mode": "real",
        "source": "竞彩官方（中国体育彩票公开赔率）",
        "source_status": "CONNECTED",
        "generated_at": observed_at,
        "match_count": len(matches),
        "market_note": "当前仅采集胜平负（1X2）；其余玩法待扩展，前端不做伪造",
        "matches": matches,
    }


class FastPollWorker:
    """1s 高频采集 worker。"""

    def __init__(self, cfg_provider, base_interval: float = 1.0) -> None:
        self.cfg_provider = cfg_provider
        self.base_interval = base_interval
        self.interval = base_interval
        self._task: asyncio.Task | None = None
        self._bulletin_task: asyncio.Task | None = None
        self._sig: dict[str, dict] = {}
        self.last_change_at: str | None = None
        self.last_fetch_ok: bool | None = None
        self.consecutive_fail = 0
        self.poll_count = 0
        self.change_count = 0
        self.last_fetch_ms: int | None = None
        self.last_process_ms: int | None = None
        self.started_at: str | None = None
        self._feed: dict[str, Any] | None = None

    def start(self) -> None:
        if self._task is None:
            self.started_at = _now()
            self._task = asyncio.create_task(self._run())
            self._bulletin_task = asyncio.create_task(self._bulletin_loop())

    def stop(self) -> None:
        for t in (self._task, self._bulletin_task):
            if t:
                t.cancel()
        self._task = None
        self._bulletin_task = None

    def status(self) -> dict:
        return {
            "base_interval": self.base_interval,
            "current_interval": round(self.interval, 2),
            "poll_count": self.poll_count,
            "change_count": self.change_count,
            "consecutive_fail": self.consecutive_fail,
            "last_fetch_ok": self.last_fetch_ok,
            "last_fetch_ms": self.last_fetch_ms,
            "last_process_ms": self.last_process_ms,
            "last_change_at": self.last_change_at,
            "started_at": self.started_at,
            "sse_clients": bus.client_count,
            "cache_matches": len((self._feed or {}).get("matches", [])),
            "cache_generated_at": (self._feed or {}).get("generated_at"),
        }

    def snapshot(self) -> dict[str, Any] | None:
        return self._feed

    @staticmethod
    def _diff(old: dict, new: dict) -> list[dict]:
        changed: list[dict] = []
        for mid, nv in new.items():
            ov = old.get(mid)
            if nv["st"] != "2":
                if ov and ov.get("st") == "2":
                    changed.append({"match_id": mid, "kind": "closed", "new": None})
                continue
            odds = [nv["h"], nv["d"], nv["a"]]
            if ov is None:
                if all(odds):
                    changed.append({"match_id": mid, "kind": "new", "new": odds})
            elif (ov["h"], ov["d"], ov["a"], ov.get("st")) != (nv["h"], nv["d"], nv["a"], nv.get("st")):
                changed.append({"match_id": mid, "kind": "odds",
                                "old": [ov["h"], ov["d"], ov["a"]], "new": odds})
        for mid, ov in old.items():
            if ov.get("st") == "2" and mid not in new:
                changed.append({"match_id": mid, "kind": "closed", "new": None})
        return changed

    async def _run(self) -> None:
        loop = asyncio.get_event_loop()
        # 建立基线（不广播）
        try:
            cfg = self.cfg_provider()
            raw = await loop.run_in_executor(None, _tc_fb_fetch_schedule, cfg)
            self._sig = _extract_sig(raw)
            self._feed = _build_live_feed(raw)
        except Exception:
            pass
        while True:
            t0 = time.time()
            try:
                cfg = self.cfg_provider()
                tf = time.time()
                raw = await loop.run_in_executor(None, _tc_fb_fetch_schedule, cfg)
                self.last_fetch_ms = round((time.time() - tf) * 1000)
                new_sig = _extract_sig(raw)
                self.consecutive_fail = 0
                self.interval = self.base_interval
                self.last_fetch_ok = True
                self.poll_count += 1

                observed_at = _now()
                new_feed = _build_live_feed(raw, observed_at)
                changed = self._diff(self._sig, new_sig)
                self._feed = new_feed
                if changed:
                    tp = time.time()
                    result = await loop.run_in_executor(
                        None, process_tc_fb_schedule, cfg, raw)
                    self.last_process_ms = round((time.time() - tp) * 1000)
                    self.change_count += 1
                    self.last_change_at = observed_at
                    await bus.broadcast({
                        "event": "quotes_changed",
                        "changed_matches": changed,
                        "quotes_changed": result.get("quotes_changed"),
                        "match_count": new_feed.get("match_count"),
                        "feed": new_feed,
                        "worker": self.status(),
                        "server_time": observed_at,
                    })
                else:
                    # 无变化：每秒心跳，链路存活 + 实时推送延迟测量
                    await bus.broadcast({
                        "event": "heartbeat",
                        "worker": self.status(),
                        "server_time": observed_at,
                    })
                self._sig = new_sig
            except Exception as exc:  # 含网络异常/风控
                self.consecutive_fail += 1
                self.last_fetch_ok = False
                self.interval = min(
                    self.base_interval * (2 ** min(self.consecutive_fail, 5)), 30.0)
                await bus.broadcast({
                    "event": "source_status",
                    "status": "ERROR",
                    "reason": type(exc).__name__,
                    "consecutive_fail": self.consecutive_fail,
                    "retry_interval": round(self.interval, 2),
                    "server_time": _now(),
                })
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.0, self.interval - elapsed))

    async def _bulletin_loop(self) -> None:
        """公告每 5 分钟采一次（销售安排/无效场次）。"""
        loop = asyncio.get_event_loop()
        while True:
            try:
                b = await loop.run_in_executor(None, collect_bulletins)
                await bus.broadcast({
                    "event": "bulletin",
                    "status": b.get("status"),
                    "total": b.get("total"), "inserted": b.get("inserted"),
                    "server_time": _now(),
                })
            except Exception:
                pass
            await asyncio.sleep(300)


worker: FastPollWorker | None = None
hg_worker: HGDirectWorker | None = None
hg_http: HGHttpCollector | None = None


def init_worker(cfg_provider, hg_cfg_provider=None) -> FastPollWorker:
    global worker, hg_worker, hg_http
    # Bind the current asyncio loop so HGDirectWorker (a plain thread) can broadcast.
    try:
        bus.bind_loop(asyncio.get_running_loop())
    except RuntimeError:
        pass
    interval = float(os.environ.get("TC_POLL_INTERVAL", "0.5"))
    worker = FastPollWorker(cfg_provider, base_interval=interval)
    worker.start()
    if hg_cfg_provider is not None:
        try:
            hcfg = hg_cfg_provider()
            if hcfg and hcfg.get("enabled", True):
                hg_worker = HGDirectWorker(hg_cfg_provider, poll_seconds=0.05)
                hg_worker.start()
        except Exception:
            hg_worker = None
    # Independent HTTP collector (primary data path; CDP above is diagnostic/fallback)
    try:
        hg_http = HGHttpCollector()
        # V2-2: restore cache from DB BEFORE starting the poll thread, so the
        # page can read HG data before the first live HTTP round-trip.
        try:
            hg_http.restore_from_db()
        except Exception:
            pass
        if hasattr(hg_http, 'start'):
            hg_http.start()
        elif hasattr(hg_http, '_run'):
            import threading
            threading.Thread(target=hg_http._run, daemon=True).start()
    except Exception as _e:
        import traceback as _tb
        print(f"[realtime] HGHttpCollector FAILED: {_e}")
        _tb.print_exc()
        hg_http = None
    return worker


def stop_workers() -> None:
    global worker, hg_worker, hg_http
    if worker:
        worker.stop()
    if hg_worker:
        hg_worker.stop()
    if hg_http:
        hg_http.stop()

async def sse_endpoint(request: Request) -> StreamingResponse:
    q = await bus.register()

    async def event_gen():
        hello = {
            "event": "connected",
            "sse_clients": bus.client_count,
            "worker": worker.status() if worker else None,
            "hg": hg_worker.status() if hg_worker else None,
            "hg_http": hg_http.status() if hg_http else None,
            "server_time": _now(),
        }
        yield f"event: connected\ndata: {json.dumps(hello, ensure_ascii=False)}\n\n"
        try:
            while True:
                try:
                    if await request.is_disconnected():
                        break
                    try:
                        data = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"event: update\ndata: {data}\n\n"
                    except asyncio.TimeoutError:
                        yield f"event: ping\ndata: {json.dumps({'server_time': _now()})}\n\n"
                except Exception as _e:
                    yield f"event: error\ndata: {json.dumps({'error': str(_e)})}\n\n"
        finally:
            bus.unregister(q)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
