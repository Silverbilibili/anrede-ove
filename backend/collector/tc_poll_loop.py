"""TC 常驻轮询采集（后台运行）
用法: python -m backend.collector.tc_poll_loop [--interval 60] [--once]
行为: 每 interval 秒跑一次 TC 采集；采到变化就打印一行；
      11:00 开售后 matchStatus=2 的比赛会自动进入采集范围。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.collector.collector import collect  # noqa: E402
from backend.collector.tc_bulletin import collect_bulletins  # noqa: E402

CONFIG_FILE = ROOT / "config" / "sources.json"
LOG_DIR = ROOT / "data" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_tc_cfg() -> dict:
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    return next((x for x in cfg.get("sources", []) if x["name"].upper() == "TC"), None)


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_DIR / "tc_poll.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=60, help="轮询间隔秒数")
    ap.add_argument("--once", action="store_true", help="只跑一次（调试用）")
    args = ap.parse_args()

    cfg = load_tc_cfg()
    if not cfg:
        log("ERROR: TC source 未配置")
        return

    log(f"TC 轮询启动 | 模式={cfg.get('mode')} | 间隔={args.interval}s")
    last_bulletin = 0.0
    while True:
        try:
            result = collect(cfg)
            status = result.get("status")
            if status == "OK":
                log(f"采集OK | matches={result.get('matches', 0)} quotes_changed={result.get('quotes_changed', 0)}")
            else:
                log(f"采集异常 | status={status} {result}")
        except Exception as exc:
            log(f"采集失败: {exc}")

        # 公告每 5 分钟采一次（销售安排/无效场次）
        if time.time() - last_bulletin >= 300:
            try:
                b = collect_bulletins()
                log(f"公告采集 | {b.get('status')} total={b.get('total', 0)} inserted={b.get('inserted', 0)}")
            except Exception as exc:
                log(f"公告采集失败: {exc}")
            last_bulletin = time.time()

        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
