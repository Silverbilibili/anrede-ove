"""
竞彩官方公开接口高频轮询实测（仅用于评估 1s 级更新可行性）。
用法: python test_tc_poll_rate.py <间隔秒> <次数>
"""
import json, time, urllib.request, hashlib, sys

URL = "https://webapi.sporttery.cn/gateway/uniform/fb/getMatchDataPageListV1.qry?method=concern"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://m.sporttery.cn/mjc/zqsj/?tab=concern",
    "Accept": "application/json",
}


def once():
    t0 = time.time()
    req = urllib.request.Request(URL, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        body = r.read()
        code = r.status
    return code, (time.time() - t0) * 1000, body


def extract_odds(body):
    data = json.loads(body)
    out = {}
    for g in (data.get("value") or {}).get("matchInfoList") or []:
        for m in g.get("subMatchList", []):
            out[str(m.get("matchId"))] = (m.get("h"), m.get("d"), m.get("a"),
                                          str(m.get("matchStatus")))
    return out


interval = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
n = int(sys.argv[2]) if len(sys.argv) > 2 else 15

results, prev_odds = [], None
for i in range(n):
    try:
        code, dt, body = once()
        hsh = hashlib.md5(body).hexdigest()[:8]
        odds = extract_odds(body)
        note = ""
        if prev_odds is not None:
            for mid, o in odds.items():
                if mid in prev_odds and prev_odds[mid][:3] != o[:3]:
                    note += f"\n     变盘 mid={mid}: {prev_odds[mid][:3]} -> {o[:3]}"
        prev_odds = odds
        print(f"[{i+1:02d}] code={code} {dt:6.0f}ms len={len(body)} hash={hsh} "
              f"matches={len(odds)}{note}")
        results.append((code, dt))
    except Exception as e:
        print(f"[{i+1:02d}] ERROR {type(e).__name__}: {e}")
        results.append(None)
    if i < n - 1:
        time.sleep(interval)

ok = [r for r in results if r and r[0] == 200]
if ok:
    lats = sorted(r[1] for r in ok)
    print(f"\n成功率 {len(ok)}/{n} | 响应 min={lats[0]:.0f} "
          f"p50={lats[len(lats)//2]:.0f} max={lats[-1]:.0f} ms")
else:
    print("\n全部失败，可能触发风控或网络异常")
