"""Dafabet 浏览器驻留会话（CDP 长连接，秒级轮询）

背景：Dafabet 赛前页每次冷启动采集约 48s（重启 AdsPower 环境 + 暖场 + 导航）。
驻留模式把浏览器启动一次并保持，之后周期读 DOM：
  - 页面自身刷新赔率 → 文本哈希变化 → 只解析变化批次
  - 页面长期不刷新 → 定时 reload 兜底
  - CDP 断开 → 自动重新拉起会话

用法：
    s = DafabetSession(source_cfg)
    s.start()
    while True:
        changed = s.poll_once()      # True 表示文本变化（已入库）
        time.sleep(interval)
"""
from __future__ import annotations

import hashlib
import time
from typing import Any

from backend.collector.collector import (  # noqa: E402
    now_iso,
    store_dafabet_text,
)
from backend.collector.adspower_api import load_token, api_get  # noqa: E402


class DafabetSession:
    def __init__(self, source_cfg: dict[str, Any]):
        self.cfg = source_cfg
        self.user_id = source_cfg.get("adspower_user_id", "k1ho30ir")
        self.page_url = source_cfg.get("page_url", "https://sports.dafabet.com/en/sports/240-FOOT")
        self.warm_url = "https://sports.dafabet.com/en/live/sport/240-FOOT"
        self.wait_s = float(source_cfg.get("page_wait_seconds", 15.0))
        self._client = None
        self._debug_port = None
        self._last_hash = None
        self._polls = 0
        self._ready = False

    # ---------- 生命周期 ----------
    def start(self) -> None:
        """拉起 AdsPower 环境 + CDP 连接 + 暖场 + 导航赛前页。"""
        import urllib.request as _ur
        import json as _json
        from backend.collector.cdp_client import CDPClient

        # 干净启动：先停旧实例
        try:
            api_get(f"/api/v1/browser/stop?user_id={self.user_id}", load_token())
            time.sleep(2)
        except Exception:
            pass
        r = api_get(f"/api/v1/browser/start?user_id={self.user_id}", load_token())
        if r.get("code") != 0:
            raise RuntimeError(f"adspower start failed: {r.get('msg')}")
        self._debug_port = r["data"]["debug_port"]
        time.sleep(4)
        pages = _json.loads(_ur.urlopen(f"http://127.0.0.1:{self._debug_port}/json/list", timeout=10).read())
        page_ws = next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page")
        self._client = CDPClient.connect(page_ws)
        # 暖场（live 页稳定）→ 赛前页 → Matches
        self._client.goto(self.warm_url, wait_seconds=10)
        time.sleep(3)
        self._goto_prematch()
        self._ready = True
        self._last_hash = None

    def _goto_prematch(self) -> None:
        from backend.collector.collector import _DF_NAV_CLICK_JS, _DF_TZ_JS
        self._client.goto(self.page_url, wait_seconds=self.wait_s)
        self._client.evaluate(_DF_NAV_CLICK_JS)
        time.sleep(6)
        # 滚动触发懒加载
        for _ in range(3):
            self._client.evaluate("window.scrollBy(0, 600)")
            time.sleep(1)

    def _read_text(self) -> str:
        text = self._client.dom_text(30000)
        if not text:
            time.sleep(6)
            text = self._client.dom_text(30000)
        return text

    # ---------- 轮询 ----------
    def poll_once(self, reload_every: int = 10) -> dict[str, Any]:
        """读一次 DOM；文本变化 → 解析入库。返回报告。"""
        if not self._ready:
            self.start()
        try:
            if self._polls > 0 and self._polls % reload_every == 0:
                # 定期重导航，防页面僵死/长期不刷新
                self._goto_prematch()
            text = self._read_text()
            h = hashlib.sha256(text.encode("utf-8")).hexdigest() if text else ""
            if not text or h == self._last_hash:
                self._polls += 1
                return {"changed": False, "reason": "no_change" if text else "empty_page"}
            self._last_hash = h
            tz_data = {}
            try:
                import json as _json
                tz_info = self._client.evaluate(_TZ_JS)
                tz_data = _json.loads(tz_info or "{}")
            except Exception:
                pass
            store = store_dafabet_text(
                text,
                tz=tz_data.get("tz"),
                epoch=tz_data.get("epoch"),
                source_cfg=self.cfg,
            )
            self._polls += 1
            return {"changed": True, **store}
        except Exception as exc:
            # CDP 断开/页面异常 → 重建会话
            self.close()
            self._ready = False
            try:
                self.start()
            except Exception as e2:
                return {"changed": False, "reason": f"reconnect failed: {e2}", "error": str(exc)[:120]}
            return {"changed": False, "reason": "reconnected", "error": str(exc)[:120]}

    def close(self) -> None:
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass
        self._client = None
        self._ready = False


_TZ_JS = """JSON.stringify({tz: Intl.DateTimeFormat().resolvedOptions().timeZone,
                              epoch: Date.now()})"""
