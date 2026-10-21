"""HG independent HTTP collector (does not need CDP for data, only for session bootstrap).

Flow:
  1. One-time: pull cookies + page.ver + uid from the attached Chrome CDP target.
  2. Poll POST https://m829.crw066.com/transform.php?p=get_game_list on a thread.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from backend.hg_direct import extract_hg_xml_matches, _calc_rates

TRANSFORM: str = "https://m829.crw066.com/transform.php"
HOME_URL: str = "https://m829.crw066.com/"
COOKIE_FILE: str = "hg_cookies.json"
VER_FILE: str = "hg_ver.txt"
UID_FILE: str = "hg_uid.txt"
TPL_FILE: str = "hg_post_template.txt"
SESS_FILE: str = "hg_session.json"
RAW_FILE: str = "hg_raw_response.xml"


def _now() -> int:
    return int(time.time() * 1000)


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()



class HGHttpCollector:
    """Standalone HTTP poller for HG game list API."""

    def __init__(self, runtime_dir: str | None = None, home_url: str = HOME_URL) -> None:
        self.TRANSFORM = TRANSFORM
        self._home = home_url
        self._runtime = runtime_dir
        self._bootstrapped: bool = False
        self.session_expired: bool = False
        self._uid: str = ""
        self._ver: str = ""
        self._post_template: str = ""
        self._session: Optional[requests.Session] = None
        self._feed: list[dict] = []
        self._league_ids: list[str] = []
        self._thread: Optional[threading.Thread] = None
        self._stop: threading.Event = threading.Event()
        self.last_error: str = ""
        self._last_healthy: float = 0.0
        self._backoff_until: float = 0.0
        self.request_count: int = 0
        self.change_count: int = 0
        self.fail_streak: int = 0
        self.cache_restored: bool = False
        self.cache_restored_at: Optional[float] = None
        self.cache_stale_count: int = 0
        self.cache_source: str = ""
        self.last_valid_event_at: Optional[float] = None

    def _rt_path(self, name: str) -> str:
        base = self._runtime or os.path.join(os.path.dirname(__file__), "..", "runtime")
        return os.path.join(base, name)

    # ---- persistence helpers ----
    def _save_file(self, name: str, content: str) -> None:
        try:
            path = self._rt_path(name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception:
            pass

    def _load_file(self, name: str) -> Optional[str]:
        try:
            path = self._rt_path(name)
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return f.read().strip()
        except Exception:
            pass
        return None

    # ---- saved-template bootstrap ----
    def _try_saved_template(self) -> bool:
        try:
            uid = self._load_file(UID_FILE)
            ver = self._load_file(VER_FILE)
            tpl = self._load_file(TPL_FILE)
            if not uid or not ver or not tpl:
                return False
            self._uid = uid
            self._ver = ver
            self._post_template = tpl
            # Try a quick poll to verify
            s = requests.Session()
            s.trust_env = False
            s.proxies.update({"http": None, "https": None})
            s.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/x-www-form-urlencoded"})
            r = s.post(self.TRANSFORM, data=tpl, timeout=10)
            if r.status_code == 200 and (r.text and ("<game" in r.text or "GAME_" in r.text)):
                self._session = s
                self._bootstrapped = True
                self.session_expired = False
                print("[hg_http] Restored session from saved template")
                return True
        except Exception as e:
            print(f"[hg_http] Template restore failed: {e}")
        return False

    # ---- HTTP login ----
    def _http_login(self) -> bool:
        """Pure HTTP login to refresh uid + session cookies."""
        try:
            import base64 as _b64
            s = self._session or requests.Session()
            s.trust_env = False
            s.proxies.update({"http": None, "https": None})
            s.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/x-www-form-urlencoded",
            })
            home = self._home or HOME_URL
            s.headers["Origin"] = home
            s.headers["Referer"] = home
            s.headers["X-Requested-With"] = "XMLHttpRequest"
            try:
                s.get(home, timeout=10)
            except Exception:
                pass
            ua_b64 = _b64.b64encode(b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36").decode()
            h = dict(s.headers)
            h["Content-Type"] = "application/x-www-form-urlencoded"
            h["Origin"] = home
            h["Referer"] = home
            import re as _re
            ver = self._ver or "7f61a85292d7ce95165d15c0bf1cd21f_1792043030050"
            try:
                rv = s.post(home, data={"detection":"Y","sub_doubleLogin":"","isapp":"","q":"","appversion":""}, headers=dict(s.headers), timeout=10)
                vm = _re.search(r"top\.ver\s*=\s*'([^']+)'", rv.text)
                if vm:
                    ver = vm.group(1)
                    self._ver = ver
                    print(f"[hg_http] Fresh ver: {ver[:60]}...")
            except Exception as e:
                print(f"[hg_http] ver extraction failed: {e}")

            body = f"p=chk_login&langx=zh-cn&ver={ver}&username=ximeimei1&password=Aa123456&app=N&auto=DGAZDF&userAgent={ua_b64}"
            r = None
            for login_url in (self.TRANSFORM, f"{home}transform.php"):
                try:
                    r = s.post(login_url, data=body, headers=h, timeout=15)
                    if r.status_code == 200:
                        break
                except Exception:
                    continue
            if r is None:
                raise RuntimeError("All login URLs failed")
            m = _re.search(r"<uid>([^<]+)</uid>", r.text) or _re.search(r'"uid"\s*:\s*"([^"]+)"', r.text)
            if m:
                self._uid = m.group(1)
                self._session = s
                try:
                    mem = f"p=memSet&ver={ver}&uid={self._uid}&langx=zh-cn&action=check"
                    s.post(self.TRANSFORM, data=mem, headers=h, timeout=10)
                except Exception:
                    pass
                try:
                    ts_now = str(_now())
                    tpl = f"uid={self._uid}&ver={self._ver or ver}&langx=zh-cn&p=get_game_list&p3type=&date=&gtype=ft&showtype=live&rtype=rb&ltype=3&filter=&cupFantasy=N&sorttype=L&specialClick=&isFantasy=N&ts={ts_now}"
                    self._post_template = tpl
                    self._bootstrapped = True
                    self.session_expired = False
                    self._save_file(UID_FILE, self._uid)
                    self._save_file(VER_FILE, self._ver or ver)
                    self._save_file(TPL_FILE, self._post_template)
                    print(f"[hg_http] Login OK, uid persisted")
                except Exception:
                    pass
                return True
        except Exception as e:
            self.last_error = "http_login: " + type(e).__name__ + ": " + str(e)
            print(f"[hg_http] Login FAILED: {e}")
        return False

    # ---- restore from DB (cache) ----
    def restore_from_db(self) -> bool:
        """Try to restore feed cache from runtime files."""
        try:
            raw = self._load_file("hg_raw_response.xml")
            if raw:
                matches = extract_hg_xml_matches(raw)
                if not matches:
                    matches = _extract_hg_json(raw)
                if matches:
                    for m in matches:
                        import time as _ti; m["updated_at"] = int(_ti.time() * 1000)
                    self._feed = matches
                    self.cache_restored = True
                    self.cache_source = "restored"
                    return True
        except Exception:
            pass
        return False

    # ---- bootstrap from Chrome CDP ----
    def bootstrap_from_cdp(self, cdp_url: str = "http://127.0.0.1:9222") -> bool:
        """Pull cookies + ver + uid from the Chrome HG tab."""
        try:
            from backend.hg_direct import _extract_hg_uid_from_page
            result = _extract_hg_uid_from_page(cdp_url)
            if result and result.get("uid"):
                self._uid = result["uid"]
                self._ver = result.get("ver", self._ver)
                self._post_template = result.get("post_template", self._post_template)
                cookies = result.get("cookies", {})
                s = requests.Session()
                s.trust_env = False
                s.proxies.update({"http": None, "https": None})
                for name, value in cookies.items():
                    s.cookies.set(name, value)
                self._session = s
                self._bootstrapped = True
                self.session_expired = False
                self._save_file(UID_FILE, self._uid)
                self._save_file(VER_FILE, self._ver)
                self._save_file(TPL_FILE, self._post_template)
                print("[hg_http] Bootstrapped from CDP")
                return True
        except Exception as e:
            self.last_error = "cdp_bootstrap: " + type(e).__name__ + ": " + str(e)
        return False

    # ---- status ----
    def status(self) -> dict:
        return {
            "bootstrapped": self._bootstrapped,
            "session_expired": self.session_expired,
            "request_count": self.request_count,
            "change_count": self.change_count,
            "fail_streak": self.fail_streak,
            "last_error": self.last_error,
            "cache_restored": self.cache_restored,
            "cache_stale_count": self.cache_stale_count,
            "cache_source": self.cache_source,
            "last_valid_event_at": self.last_valid_event_at,
        }

    def snapshot(self) -> dict:
        return {
            "matches": list(self._feed),
            "league_ids": list(getattr(self, "_league_ids", [])),
            "league_count": len(getattr(self, "_league_ids", [])),
            "source_status": "CONNECTED" if self._bootstrapped else "DISCONNECTED",
            "generated_at": _ts(),
        }



    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="HGHttpPoll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        if not self._bootstrapped:
            if not self._http_login():
                self._try_saved_template()
        # Always seed the full league list (uid/ver loaded from files above)
        try:
            if self._uid and self._ver:
                self.fetch_all_leagues()
        except Exception:
            pass
        while not self._stop.is_set():
            if self._backoff_until and time.time() < self._backoff_until:
                self._stop.wait(1.0)
                continue
            self._poll_once()
            self._stop.wait(0.5)


    def fetch_all_leagues(self) -> list[str]:
        # Pure-HTTP: full league list (no wager-session conflict).
        try:
            if not self._uid or not self._ver:
                self.last_error = 'no uid/ver for get_league_list_All'
                return []
            url = self.TRANSFORM + '?ver=' + self._ver
            body = 'p=get_league_list_All&uid=' + self._uid + '&ver=' + self._ver + '&langx=zh-cn&gtype=FT&FS=N&showtype=fu&rtype=r&date=all&nocp=N'
            s = requests.Session()
            s.trust_env = False
            s.proxies.update({'http': None, 'https': None})
            s.headers.update({
                'User-Agent': 'Mozilla/5.0',
                'Origin': self._home or 'https://m829.crw066.com',
                'Referer': (self._home or 'https://m829.crw066.com/') + '/',
                'Content-Type': 'application/x-www-form-urlencoded',
            })
            r = s.post(url, data=body, timeout=20)
            text = r.text
            self._save_file('league_list_raw.txt', text)
            ids = re.findall(r'<league name=[^>]* id=([0-9]+)[^>]*>', text)
            self._league_ids = ids
            self._last_healthy = time.time()
            self.fail_streak = 0
            return ids
        except Exception as e:
            self.last_error = str(e)
            return []

    def _poll_once(self) -> None:
        if not self._session or not self._bootstrapped:
            if not self._http_login():
                if not self.bootstrap_from_cdp():
                    time.sleep(3.0)
                    return
        self.request_count += 1
        try:
            r = self._session.post(
                self.TRANSFORM,
                data=self._post_template,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0",
                },
                timeout=10,
            )
            # Save raw response
            self._save_file(RAW_FILE, f"STATUS={r.status_code} LEN={len(r.text)}\n{r.text}")
            text = r.text
            if ("VariableStandard" in text or "doubleLogin" in text
                    or r.status_code in (401, 403, 429)):
                # get_game_list blocked (doubleLogin/params) - fall back to pure-HTTP league list
                if self._uid and self._ver and not self._league_ids:
                    self.fetch_all_leagues()
                self.session_expired = False
                return
            if "table id error" in text or "<code>error</code>" in text:
                self.session_expired = True
                self._bootstrapped = False
                return
            matches = extract_hg_xml_matches(text)
            if not matches:
                matches = _extract_hg_json(text)
            if not matches:
                return
            for m in matches:
                m["updated_at"] = _now()
            self._feed = matches
            self._last_healthy = time.time()
            self.fail_streak = 0
        except Exception as e:
            self.fail_streak += 1
            if self.fail_streak > 3:
                self._backoff_until = time.time() + min(30, 2 ** self.fail_streak)



# ---- module-level helper ----
def _extract_hg_json(text: str) -> list[dict]:
    """Parse HG JSON game list response into match dicts."""
    if not text or "{" not in text:
        return []
    try:
        body = text
        if "STATUS=" in body[:100]:
            body = body[body.index("{"):]
        data = __import__("json").loads(body)
    except Exception:
        return []
    resp = data.get("response", data)
    matches = []
    for k, v in resp.items():
        if not k.startswith("GAME_"):
            continue
        gtime = v.get("GAME_DATE_TIME", "")
        home = str(v.get("GNUM_H", v.get("H", "")))
        away = str(v.get("GNUM_C", v.get("C", "")))
        comp = str(v.get("MS", v.get("COMP", "")))
        mid = str(v.get("MID", v.get("GIDM", "")))
        running = v.get("RUNNING", "N") == "Y" or v.get("PSTATUS", "") == "1"
        score_h = str(v.get("SCORE_H", "0"))
        score_a = str(v.get("SCORE_A", "0"))
        m = {
            "match_id": mid,
            "home": home,
            "away": away,
            "competition": comp,
            "kickoff_time": gtime,
            "running": running,
            "score_h": score_h,
            "score_a": score_a,
            "quotes": {},
            "markets": {},
            "extra_odds": {},
            "raw_tags": {},
        }
        # Extract IOR/RATIO tags from PLAY blocks
        extra = {}
        for pk in ("PLAY1", "PLAY3", "PLAY4", "PLAY5", "PLAY6", "PLAY7", "PLAY8"):
            play = v.get(pk, {})
            if not isinstance(play, dict):
                continue
            for mk_name, mk_data in play.items():
                if not isinstance(mk_data, dict):
                    continue
                for tag, val in mk_data.items():
                    if tag.startswith("IOR_") or tag.startswith("RATIO_"):
                        extra[tag] = str(val)
        if extra:
            m["extra_odds"] = extra
            m["raw_tags"] = extra
        matches.append(m)
    return matches




    # ---- polling loop ----
# Module-level convenience
_hg_collector: Optional[HGHttpCollector] = None


def get_collector(runtime_dir: str | None = None) -> HGHttpCollector:
    global _hg_collector
    if _hg_collector is None:
        _hg_collector = HGHttpCollector(runtime_dir=runtime_dir)
    return _hg_collector

