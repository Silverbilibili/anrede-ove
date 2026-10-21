"""CDP 浏览器通道（通用，供 HG/其他需真实浏览器的采集使用）

实现: 原生 WebSocket CDP（不依赖 playwright/greenlet）
用法:
    from backend.collector.cdp_client import CDPClient
    c = CDPClient.launch(port=9337, profile_dir=..., headless=False)
    html = c.goto_and_dom(url, wait_seconds=5)          # 导航并取渲染 DOM
    value = c.evaluate("JSON.stringify(window.__data)")  # 页面上下文取数据
    c.close()
或连接已有浏览器（AdsPower / Chrome --remote-debugging-port）:
    c = CDPClient.connect(ws_url)
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request

from websocket import create_connection


class CDPClient:
    def __init__(self, ws_url: str, proc=None):
        self.ws_url = ws_url
        self.proc = proc
        self._msg_id = 1
        self._ws = create_connection(ws_url, timeout=60, suppress_origin=True)

    # ---------- 启动 / 连接 ----------
    @classmethod
    def launch(cls, port: int, profile_dir: str, headless: bool = False,
               chrome_path: str = r"C:\Program Files\Google\Chrome\Application\chrome.exe") -> "CDPClient":
        args = [
            chrome_path, "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--remote-allow-origins=*",
            "--window-position=-2000,-2000", "--window-size=800,600",
            f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
            "about:blank",
        ]
        if headless:
            args.insert(1, "--headless=new")
        proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ws_url = None
        for _ in range(40):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
                    pages = json.loads(r.read())
                for p in pages:
                    if p.get("type") == "page":
                        ws_url = p["webSocketDebuggerUrl"]
                        break
                if ws_url:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws_url:
            proc.kill()
            raise RuntimeError(f"CDP 启动失败: port={port}")
        return cls(ws_url, proc=proc)

    @classmethod
    def connect(cls, ws_url: str) -> "CDPClient":
        return cls(ws_url)

    # ---------- CDP 基础 ----------
    def _call(self, method: str, params: dict | None = None) -> dict:
        mid = self._msg_id
        self._msg_id += 1
        self._ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            resp = json.loads(self._ws.recv())
            if resp.get("id") == mid:
                return resp

    # ---------- 页面操作 ----------
    def set_ua(self, ua: str) -> None:
        self._call("Network.enable")
        self._call("Network.setUserAgentOverride", {"userAgent": ua})

    def goto(self, url: str, wait_seconds: float = 5.0) -> None:
        self._call("Page.enable")
        self._call("Runtime.enable")
        self._call("Page.navigate", {"url": url})
        time.sleep(wait_seconds)

    def evaluate(self, expression: str) -> str:
        r = self._call("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": True,
        })
        if "exceptionDetails" in r.get("result", {}):
            raise RuntimeError(f"页面 JS 异常: {json.dumps(r['result']['exceptionDetails'], ensure_ascii=False)[:300]}")
        return r.get("result", {}).get("result", {}).get("value", "")

    def dom_text(self, max_len: int = 2000) -> str:
        return self.evaluate(f"document.body ? document.body.innerText.slice(0, {max_len}) : 'NO_BODY'")

    def wait_for(self, expression: str, timeout_seconds: float = 30.0, poll: float = 0.5) -> str:
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            try:
                v = self.evaluate(expression)
                if v not in ("", "-1", "false", "undefined"):
                    return v
            except Exception:
                pass
            time.sleep(poll)
        return ""

    # ---------- 网络抓包 ----------
    def collect_requests(self, url: str, wait_seconds: float = 10.0,
                         url_filter: str = "") -> list[dict]:
        """导航到 url，收集期间发出的网络请求（method/url）。
        url_filter: 只保留 URL 包含该子串的请求（如 '.qry' / 'api' / 'json'）。
        """
        self._call("Network.enable")
        captured = []
        self._ws.send(json.dumps({"id": 999001, "method": "Network.enable", "params": {}}))
        self._call("Page.navigate", {"url": url})
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            try:
                self._ws.settimeout(1.0)
                msg = json.loads(self._ws.recv())
            except Exception:
                continue
            method = msg.get("method", "")
            if method == "Network.requestWillBeSent":
                req = msg.get("params", {}).get("request", {})
                u = req.get("url", "")
                if url_filter and url_filter not in u:
                    continue
                captured.append({"method": req.get("method", ""), "url": u})
            elif method == "Network.responseReceived":
                pass
        seen = []
        for x in captured:
            if x not in seen:
                seen.append(x)
        return seen

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    # 自检：启动有头 Chrome，打开 data: 页面，执行 JS
    import tempfile
    c = CDPClient.launch(port=9339, profile_dir=tempfile.mkdtemp(prefix="cdp_selfcheck_"))
    try:
        c.goto("data:text/html,<html><body><div id='x'>42</div></body></html>", wait_seconds=1)
        print("evaluate:", c.evaluate("document.getElementById('x').innerText"))
    finally:
        c.close()
    print("CDPClient OK")
