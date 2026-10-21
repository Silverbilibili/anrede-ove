#!/usr/bin/env python3
"""HG 完整流程诊断脚本 — 登录 → memSet → get_game_list → 解析。"""
import requests, sys, time, base64, re

VER = "b27b446edf512037017ecb25371b6de1_1791436544038"
BASE = "https://www.hga050.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

s = requests.Session()
s.trust_env = False
s.proxies.update({"http": None, "https": None})
s.headers["User-Agent"] = UA
s.headers["Content-Type"] = "application/x-www-form-urlencoded"
s.headers["Origin"] = BASE
s.headers["Referer"] = BASE + "/"
s.headers["X-Requested-With"] = "XMLHttpRequest"

r0 = s.get(BASE + "/", timeout=10)
print("home status:", r0.status_code, "cookies:", len(s.cookies))

ua_b64 = base64.b64encode(UA.encode()).decode()
body = f"p=chk_login&langx=zh-cn&ver={VER}&username=ximeimei1&password=Aa123456&app=N&auto=DGAZDF&userAgent={ua_b64}"
r = s.post(BASE + "/transform_nl.php", data=body, timeout=15)
uid = re.search(r"<uid>([^<]+)</uid>", r.text).group(1) if "<uid>" in r.text else ""
print("login:", r.status_code, "len:", len(r.text))
print("uid:", uid)

s.post(BASE + "/transform_nl.php", data=f"p=memSet&ver={VER}&uid={uid}&langx=zh-cn&action=check", timeout=10)

ts = str(int(time.time() * 1000))
gl = (f"uid={uid}&ver={VER}&langx=zh-cn&p=get_game_list&p3type=&date="
      f"&gtype=ft&showtype=live&rtype=rb&ltype=3&filter=&cupFantasy=N"
      f"&sorttype=L&specialClick=&isFantasy=N&ts={ts}")
r2 = s.post(BASE + "/transform.php", data=gl, timeout=15)
print("get_game_list:", r2.status_code, "len:", len(r2.text))
print("first150:", r2.text[:150].replace("\n", " "))
print("GID:", r2.text.count("<GID>"), "IOR:", r2.text.count("IOR_"))
print("DONE")
