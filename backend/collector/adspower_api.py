"""AdsPower 本地 API 客户端（签名认证）
用法:
  python -m backend.collector.adspower_api list
  python -m backend.collector.adspower_api start <user_id>
"""
from __future__ import annotations

import base64
import json
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

API_BASE = "http://127.0.0.1:50325"


def load_token() -> str:
    """token 读取优先级：环境变量 ADS_POWER_TOKEN → 包外 secrets/ → 包内 config/（警告降级）。

    包外 secrets 目录位于工程根同级（C:\\1\\football_hedge_p0\\secrets），不随交付包分发。
    """
    import os
    env = os.environ.get("ADS_POWER_TOKEN", "").strip()
    if env:
        return env
    outer = ROOT.parent / "secrets" / "adspower_token.txt"  # 工程根同级 secrets
    if outer.exists():
        return outer.read_text(encoding="utf-8").strip()
    inner = ROOT / "config" / "adspower_token.txt"
    if inner.exists():
        print("[warn] token 位于包内 config/，请移出到包外 secrets/（工程根同级）", file=sys.stderr)
        return inner.read_text(encoding="utf-8").strip()
    return ""


def signed_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def api_get(path: str, token: str) -> dict:
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url, headers=signed_headers(token))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def api_post(path: str, token: str, payload: dict) -> dict:
    url = f"{API_BASE}{path}"
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=signed_headers(token), method="POST")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def list_users(token: str) -> dict:
    return api_get("/api/v1/user/list?page=1&page_size=100", token)


def start_user(token: str, user_id: str) -> dict:
    return api_get(f"/api/v1/user/start?user_id={urllib.parse.quote(user_id)}", token)


def main() -> None:
    token = load_token()
    if not token:
        print("缺少 token：请把 AdsPower API Token 写入 config/adspower_token.txt")
        return
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        r = list_users(token)
        print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "start":
        uid = sys.argv[2]
        r = start_user(token, uid)
        print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
