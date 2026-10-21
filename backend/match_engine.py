"""
HG-TC multi-dimensional match engine.
Fuzzy team name matching + kickoff time + known translation map.
"""
import re
from datetime import datetime

KNOWN_MAP = {
    "神户胜利船": ["vissel kobe", "神户胜利船"],
    "全北现代": ["jeonbuk hyundai", "全北现代"],
    "蔚山现代": ["ulsan hyundai", "蔚山现代"],
    "横滨水手": ["yokohama f marinos", "横滨水手"],
    "川崎前锋": ["kawasaki frontale", "川崎前锋"],
    "浦和红钻": ["urawa red diamonds", "浦和红钻"],
    "水户蜀葵": ["mito hollyhock", "水户蜀葵"],
    "清水心跳": ["shimizu s pulse", "清水心跳"],
    "东京": ["fc tokyo", "东京FC"],
    "拜仁": ["bayern munich", "拜仁慕尼黑"],
    "多特蒙德": ["borussia dortmund", "多特蒙德"],
    "皇马": ["real madrid", "皇家马德里"],
    "巴萨": ["barcelona", "巴塞罗那"],
    "马竞": ["atletico madrid", "马德里竞技"],
    "曼城": ["manchester city", "曼城"],
    "曼联": ["manchester united", "曼联"],
    "利物浦": ["liverpool", "利物浦"],
    "阿森纳": ["arsenal", "阿森纳"],
    "切尔西": ["chelsea", "切尔西"],
    "热刺": ["tottenham hotspur", "热刺"],
    "尤文图斯": ["juventus", "尤文图斯"],
    "AC米兰": ["ac milan", "AC米兰"],
    "国米": ["inter milan", "国际米兰"],
    "那不勒斯": ["napoli", "那不勒斯"],
    "巴黎圣日耳曼": ["paris saint germain", "巴黎"],
    "费耶诺德": ["feyenoord", "费耶诺德"],
    "博德闪耀": ["bodo glimt", "博德闪耀"],
    "莱比锡": ["rb leipzig", "莱比锡红牛"],
    "罗马": ["roma", "罗马"],
}

def norm(name: str) -> str:
    n = name.lower().strip()
    n = re.sub(r"[-]", " ", n)
    n = re.sub(r"[^a-z0-9]", "", n)
    return n.replace(" ", "")

def edit_dist(a: str, b: str) -> float:
    if not a or not b: return 0.0
    if a == b: return 1.0
    la, lb = len(a), len(b)
    dp = [[0]*(lb+1) for _ in range(la+1)]
    for i in range(la+1): dp[i][0] = i
    for j in range(lb+1): dp[0][j] = j
    for i in range(1, la+1):
        for j in range(1, lb+1):
            dp[i][j] = min(dp[i-1][j]+1, dp[i][j-1]+1, dp[i-1][j-1]+(0 if a[i-1]==b[j-1] else 1))
    return 1.0 - dp[la][lb]/max(la,lb)

def fuzzy_match_teams(hg_h: str, hg_a: str, tc_h: str, tc_a: str) -> float:
    h_ok = any(norm(a) in norm(tc_h) or norm(tc_h) in norm(a) for a in KNOWN_MAP.get(hg_h,[]))
    a_ok = any(norm(a) in norm(tc_a) or norm(tc_a) in norm(a) for a in KNOWN_MAP.get(hg_a,[]))
    if h_ok and a_ok: return 0.95
    nh, na = norm(hg_h), norm(hg_a)
    ntch, ntca = norm(tc_h), norm(tc_a)
    d1 = max(edit_dist(nh,ntch), edit_dist(nh,ntca))
    d2 = max(edit_dist(na,ntca), edit_dist(na,ntch))
    nm = max((d1+d2)/2, edit_dist(nh+na, ntch+ntca)/2)
    if h_ok or a_ok: nm = max(nm, 0.6)
    return nm

def time_prox(t1: str, t2: str) -> float:
    if not t1 or not t2: return 0.5
    try:
        t1c = t1.replace("a", " AM").replace("p", " PM")
        d1 = datetime.strptime(t1c, "%m-%d %I:%M %p")
        d2 = datetime.fromisoformat(t2.replace("Z", "+00:00"))
        diff = abs((d1 - d2.replace(tzinfo=None)).total_seconds())/60
        if diff <= 5: return 1.0
        if diff <= 15: return 0.8
        if diff <= 60: return 0.5
        return max(0, 0.3 - diff/600)
    except: return 0.5

def match_score(hg: dict, tc: dict) -> float:
    n = fuzzy_match_teams(hg.get("home",""),hg.get("away",""),tc.get("home",""),tc.get("away",""))
    t = time_prox(hg.get("kickoff_time"),tc.get("kickoff_time_utc"))
    return n*0.7 + t*0.3

def match_all(hg_matches: list, tc_matches: list, threshold=0.55) -> list:
    results = []
    for hm in hg_matches:
        best, best_tc = 0.0, None
        for tm in tc_matches:
            s = match_score(hm, tm)
            if s > best: best, best_tc = s, tm
        if best >= threshold and best_tc:
            results.append({"tc_id":best_tc["match_id"],"hg_id":hm["match_id"],"score":round(best,3)})
    results.sort(key=lambda x:-x["score"])
    return results