"""HG direct/CDP collector.

Reads only network responses/frames already received by a user-controlled,
locally running Chrome CDP session. It does not perform login, credential
handling, proxy rotation, CAPTCHA bypass, or access-control bypass.

The collector is intentionally schema-tolerant because the HG payload shape
is site/version dependent. It keeps the last normalized 1X2 snapshot in
memory and emits a change event to backend.realtime.EventBus.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import threading
import time
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlparse

import requests
import websocket
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

HOME_KEYS = (
    "home", "homeTeam", "home_team", "homeName", "home_name",
    "teamHome", "homeTeamName", "home_team_name", "ht", "homeTeamAbbName",
)
AWAY_KEYS = (
    "away", "awayTeam", "away_team", "awayName", "away_name",
    "teamAway", "awayTeamName", "away_team_name", "at", "awayTeamAbbName",
)
MATCH_ID_KEYS = (
    "matchId", "match_id", "eventId", "event_id", "gameId", "game_id",
    "fixtureId", "fixture_id", "id", "fid", "eventNo", "event_no",
)
LEAGUE_KEYS = (
    "league", "leagueName", "league_name", "competition", "competitionName",
    "leagueAbbName", "leagueAllName", "tournament", "comp",
)
KICKOFF_KEYS = (
    "kickoff", "kickoffTime", "kickoff_time", "startTime", "start_time",
    "matchTime", "match_time", "eventTime", "event_time",
)
SPORT_KEYS = ("sport", "sportName", "sport_name", "gameType", "category")
ODDS_CONTAINER_KEYS = ("odds", "prices", "price", "markets", "market", "selections", "lines", "quote")

HOME_ODDS_KEYS = ("home", "1", "one", "h", "homeOdds", "oddsHome", "homePrice")
DRAW_ODDS_KEYS = ("draw", "x", "tie", "d", "drawOdds", "oddsDraw", "drawPrice")
AWAY_ODDS_KEYS = ("away", "2", "two", "a", "awayOdds", "oddsAway", "awayPrice")


def _csv_env(name: str, default: str = "") -> list[str]:
    raw = os.environ.get(name, default)
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


def _is_hg_transform_url(url: str, cfg: dict[str, Any] | None = None) -> bool:
    if not url:
        return False
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        path = (u.path or "").lower().rstrip("/") or "/"
        cfg = cfg or {}
        hosts = [str(h).lower().lstrip(".") for h in (cfg.get("data_hosts") or _csv_env("HG_DATA_HOSTS", "crw066.com"))]
        paths = [str(p).lower().rstrip("/") or "/" for p in (cfg.get("transform_paths") or _csv_env("HG_TRANSFORM_PATHS", "/transform.php,/transform_nl.php"))]
        host_ok = any(host == h or host.endswith("." + h) for h in hosts)
        path_ok = any(path == p or path.endswith(p) for p in paths)
        return host_ok and path_ok
    except Exception:
        return False


def _extract_jsonish(text: str) -> Any | None:
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not s or len(s) > 8_000_000:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        a, b = s.find(opener), s.rfind(closer)
        if a >= 0 and b > a:
            candidate = s[a:b + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass
    try:
        from urllib.parse import unquote_plus
        dec = unquote_plus(s)
        if dec != s:
            try:
                return json.loads(dec)
            except json.JSONDecodeError:
                pass
            for opener, closer in (("{", "}"), ("[", "]")):
                a, b = dec.find(opener), dec.rfind(closer)
                if a >= 0 and b > a:
                    try:
                        return json.loads(dec[a:b + 1])
                    except json.JSONDecodeError:
                        pass
    except Exception:
        pass
    compact = "".join(s.split())
    if len(compact) >= 32 and len(compact) % 4 == 0 and re.fullmatch(r"[A-Za-z0-9+/=]+", compact):
        try:
            decoded = base64.b64decode(compact, validate=True).decode("utf-8", "ignore").strip()
            if decoded and decoded != s:
                try:
                    return json.loads(decoded)
                except json.JSONDecodeError:
                    pass
                for opener, closer in (("{", "}"), ("[", "]")):
                    a, b = decoded.find(opener), decoded.rfind(closer)
                    if a >= 0 and b > a:
                        try:
                            return json.loads(decoded[a:b + 1])
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
    return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float, str)):
        s = str(v).strip()
        if 1 <= len(s) <= 120:
            return s
    return ""


def _looks_like_team_name(v: str) -> bool:
    s = v.strip()
    if not s or len(s) > 100:
        return False
    if _to_odds(s) is not None:
        return False
    return bool(re.search(r"[A-Za-zÀ-ÿ一-龥]", s))


def _get_first(d: dict[str, Any], keys: tuple[str, ...]) -> str:
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in d:
            s = _clean_str(d[k])
            if s:
                return s
        lk = k.lower()
        if lk in lower:
            s = _clean_str(lower[lk])
            if s:
                return s
    return ""


def _to_odds(v: Any) -> str | None:
    try:
        if isinstance(v, bool) or v is None:
            return None
        s = str(v).strip().replace(",", "")
        # reject obvious non-odds values
        if not re.fullmatch(r"\d+(?:\.\d+)?", s):
            return None
        d = Decimal(s)
        if d <= 1 or d > 1000:
            return None
        return format(d.normalize(), "f")
    except (InvalidOperation, ValueError):
        return None


def _get_first_odds(d: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    lower = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        val = d.get(k, lower.get(k.lower()))
        o = _to_odds(val)
        if o:
            return o
    return None


def _looks_like_football(d: dict[str, Any]) -> bool:
    sport = _get_first(d, SPORT_KEYS).lower()
    if not sport:
        return True
    return any(x in sport for x in ("football", "soccer", "futbol", "fußball"))


def _parse_json_payload(payload: Any) -> Any | None:
    if isinstance(payload, (dict, list)):
        return payload
    if not isinstance(payload, str):
        return None
    return _extract_jsonish(payload)


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _extract_odds_from_dict(d: dict[str, Any], max_depth: int = 3) -> tuple[str | None, str | None, str | None]:
    """Find a 1X2 triple anywhere below a match object without mistaking team fields for odds."""
    def direct(x: dict[str, Any]):
        h = _get_first_odds(x, HOME_ODDS_KEYS + ("odds1", "sp1", "home_price"))
        dr = _get_first_odds(x, DRAW_ODDS_KEYS + ("oddsx", "spx", "draw_price"))
        a = _get_first_odds(x, AWAY_ODDS_KEYS + ("odds2", "sp2", "away_price"))
        if h and dr and a:
            return h, dr, a
        # Direct array under a likely odds container.
        for key in ODDS_CONTAINER_KEYS + ("outcomes", "selectionsData", "oddsList", "sp"):
            child = x.get(key)
            if isinstance(child, list) and len(child) >= 3:
                vals = []
                for item in child[:3]:
                    if isinstance(item, dict):
                        val = item.get("odds", item.get("price", item.get("sp")))
                    else:
                        val = item
                    vals.append(_to_odds(val))
                if all(vals):
                    return vals[0], vals[1], vals[2]
        return None, None, None

    h, dr, a = direct(d)
    if h and dr and a:
        return h, dr, a
    if max_depth <= 0:
        return None, None, None
    for child in d.values():
        if isinstance(child, dict):
            found = _extract_odds_from_dict(child, max_depth - 1)
            if all(found):
                return found
        elif isinstance(child, list):
            for item in child[:50]:
                if isinstance(item, dict):
                    found = _extract_odds_from_dict(item, max_depth - 1)
                    if all(found):
                        return found
    return None, None, None


def extract_matches(payload: Any, source_url: str = "") -> list[dict[str, Any]]:
    """Best-effort 1X2 extraction from a JSON response/WS frame."""
    parsed = _parse_json_payload(payload)
    if parsed is None:
        return []

    out: list[dict[str, Any]] = []
    for d in _walk(parsed):
        if not _looks_like_football(d):
            continue
        home = _get_first(d, HOME_KEYS)
        away = _get_first(d, AWAY_KEYS)
        # Prevent nested odds dictionaries from being mistaken for a match.
        if not _looks_like_team_name(home) or not _looks_like_team_name(away):
            continue
        if home.lower() == away.lower():
            continue
        h, dr, a = _extract_odds_from_dict(d)
        if not (h and dr and a):
            continue
        mid = _get_first(d, MATCH_ID_KEYS)
        league = _get_first(d, LEAGUE_KEYS)
        kickoff = _get_first(d, KICKOFF_KEYS)
        if not mid:
            key = f"{home}|{away}|{kickoff}".lower()
            mid = "HG-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
        out.append({
            "match_id": mid,
            "competition": league,
            "home": home,
            "away": away,
            "kickoff_time": kickoff,
            "quotes": {
                "HOME": {"odds": h},
                "DRAW": {"odds": dr},
                "AWAY": {"odds": a},
            },
            "source_url": source_url,
        })

    # Deduplicate by match id and keep latest occurrence.
    dedup: dict[str, dict[str, Any]] = {}
    for item in out:
        dedup[item["match_id"]] = item
    return list(dedup.values())


def extract_hg_xml_matches(xml_text: str, source_url: str = "") -> list[dict[str, Any]]:
    """Parse HG crown XML (serverresponse/game) into multi-market match shape (V4.2).

    Extracts ALL available markets from each <game> element:
      - 1X2: IOR_RMH/IOR_RMC/IOR_RMN
      - AH:  RATIO_ROUO/IOR_ROUO/IOR_ROUH (away/home odds with line)
      - OU:  RATIO_ROUC/IOR_ROUH/IOR_ROUC (over/under odds with line)
      - Also captures: STRONG, HSTRONG, RATIO, IOR_* wildcards

    Observed HG XML tags (crown API p=get_game_list with ltype=3):
      <IOR_RMH> home 1X2 odds | <IOR_RMC> draw | <IOR_RMN> away
      <RATIO_ROUO> AH line | <IOR_ROUO> AH away | <IOR_ROUH> AH home
      <RATIO_ROUC> OU line | <IOR_ROUH> over | <IOR_ROUC> under
      <STRONG>H</STRONG> which side is favored

    Emits all matches where GOPEN=Y with at least one valid market.
    """
    if not isinstance(xml_text, str):
        return []
    s = xml_text.lstrip()
    if not (s.startswith("<?xml") or s.startswith("<serverresponse")):
        return []
    try:
        root = ET.fromstring(s)
    except ET.ParseError:
        return []

    out: list[dict[str, Any]] = []
    for game in root.iter("game"):
        def g(tag: str) -> str:
            el = game.find(tag)
            return (el.text or "").strip() if el is not None and el.text else ""

        # --- Extract all raw tags for debug/expansion ---
        raw_tags: dict[str, str] = {}
        for child in game:
            tag = child.tag.strip()
            val = (child.text or "").strip()
            if tag and val:
                raw_tags[tag] = val

        gid = g("GID") or game.get("id", "")
        home = g("TEAM_H")
        away = g("TEAM_C")
        if not gid or not home or not away:
            continue
        if home == away:
            continue
        if g("GOPEN").upper() != "Y":
            continue

        # --- Build match base ---
        match: dict[str, Any] = {
            "match_id": gid,
            "competition": g("LEAGUE"),
            "home": home,
            "away": away,
            "kickoff_time": g("DATETIME"),
            "score_h": g("SCORE_H"),
            "score_c": g("SCORE_C"),
            "match_time": g("RETIMESET"),
            "now_model": g("NOW_MODEL"),
            "running": g("RUNNING").upper() == "Y",
            "strong": g("STRONG"),        # H=home favored, C=away favored
            "hstrong": g("HSTRONG"),
            "source_url": source_url,
            "raw_tags": raw_tags,          # V4.2: all parsed tags for inspection
            "markets": {},                 # V4.2: multi-market odds dict
        }

        # --- 1X2 (European) --- (V4.2: now uses IOR_MH/MC/MN from specialClick=special)
        h1 = g("IOR_MH") or g("IOR_RMH")
        # V4.2 fix: M C=away(kè), M N=draw(píng), confirmed by user
        a1 = g("IOR_MC") or g("IOR_RMC")
        d1 = g("IOR_MN") or g("IOR_RMN")
        if h1 and d1 and a1:
            try:
                fh, fd, fa = Decimal(h1), Decimal(d1), Decimal(a1)
                if fh > 1 and fd > 1 and fa > 1:
                    line_1x2 = g("RATIO_R")
                    match["markets"]["1X2"] = {
                        "HOME": {"odds": format(fh.normalize(), "f")},
                        "DRAW": {"odds": format(fd.normalize(), "f")},
                        "AWAY": {"odds": format(fa.normalize(), "f")},
                        "line": line_1x2,
                    }
            except (InvalidOperation, ValueError):
                pass

        # --- AH (Asian Handicap) V4.2: HR / HM / HOU + legacy ROU ---
        ah_entries = []
        for prefix, line_tag in [
            ("HR",  "RATIO_HR"),
            ("HM",  "RATIO_R"),       # 3-way handicap 1X2 (ràng qiú shèng píng fù)
            ("HOU", "RATIO_HOUO"),     # fallback RATIO_HOUU below
            ("ROU", "RATIO_ROUO"),     # legacy ltype=3
        ]:
            h_tag = f"IOR_{prefix}H"; c_tag = f"IOR_{prefix}C"
            if prefix == "ROU":
                h_tag = "IOR_ROUH"; c_tag = "IOR_ROUC"
            h_val = g(h_tag); c_val = g(c_tag)
            # HM has 3 selections (HOME/DRAW/AWAY), check for third
            d_val = ""
            if prefix == "HM":
                d_val = g("IOR_HMN")
                if not d_val:
                    d_val = g("IOR_RMN")  # legacy
            # Standard 2-selection markets
            if (h_val and c_val) or (prefix == "HM" and h_val and d_val):
                try:
                    fh = Decimal(h_val); fc = Decimal(c_val)
                    if fh > Decimal("0.01") and fc > Decimal("0.01"):
                        line_val = g(line_tag) if line_tag else ""
                        if prefix == "HOU" and not line_val:
                            line_val = g("RATIO_HOUU")
                        if prefix == "ROU":
                            line_val = g("RATIO_ROUO") or g("RATIO_ROUU")
                        entry = {
                            "HOME": {"odds": format(fh.normalize(), "f")},
                            "AWAY": {"odds": format(fc.normalize(), "f")},
                            "line": line_val,
                            "code": prefix,
                        }
                        # V4.2: HM has 3 selections
                        if prefix == "HM" and d_val:
                            try:
                                fd = Decimal(d_val)
                                if fd > Decimal("0.01"):
                                    entry["DRAW"] = {"odds": format(fd.normalize(), "f")}
                            except:
                                pass
                        ah_entries.append(entry)
                except (InvalidOperation, ValueError):
                    pass
        if ah_entries:
            match["markets"]["AH"] = ah_entries

        # --- OU (Over/Under) V4.2: IOR_OUH/OUC + legacy ROU ---
        ouh = g("IOR_OUH"); ouc = g("IOR_OUC")
        if not ouh:
            ouh = g("IOR_ROUH"); ouc = g("IOR_ROUC")   # legacy
        if ouh and ouc:
            try:
                fo = Decimal(ouh); fu = Decimal(ouc)
                if fo > Decimal("0.01") and fu > Decimal("0.01"):
                    line_ou = g("RATIO_OUO") or g("RATIO_OUU") or g("RATIO_ROUC") or ""
                    match["markets"]["OU"] = {
                        "OVER":  {"odds": format(fo.normalize(), "f")},
                        "UNDER": {"odds": format(fu.normalize(), "f")},
                        "line": line_ou,
                    }
            except (InvalidOperation, ValueError):
                pass

        # --- Other markets: Even/Odd, Total Score ---
        if g("IOR_REOE") and g("IOR_REOO"):
            try:
                match["markets"]["EO"] = {
                    "EVEN": {"odds": format(Decimal(g("IOR_REOE")).normalize(), "f")},
                    "ODD":  {"odds": format(Decimal(g("IOR_REOO")).normalize(), "f")},
                }
            except: pass

        # --- V5.3: universal IOR tag scanner - captures ALL markets HG provides ---
        for tag_key, tag_val in raw_tags.items():
            if tag_key.startswith("IOR_") or tag_key.startswith("RATIO_"):
                match.setdefault("extra_odds", {})[tag_key] = tag_val
        # Auto-detect market groups from IOR/RATIO naming patterns
        _raw = {k: v for k, v in raw_tags.items() if v}
        # Goal line (RGC/H/N)
        if "IOR_RGC" in _raw and "IOR_RGH" in _raw:
            try:
                match["markets"]["GL"] = {
                    "OVER": {"odds": format(Decimal(_raw["IOR_RGC"]).normalize(), "f"), "name": "大球"},
                    "UNDER": {"odds": format(Decimal(_raw["IOR_RGH"]).normalize(), "f"), "name": "小球"},
                    "line": _raw.get("RATIO_RE", ""),
                }
            except: pass
        # Half-time 1X2 (alternative tags)
        if "IOR_HRMH" in _raw and "IOR_HRMN" in _raw:
            try:
                match["markets"]["HT1X2"] = {
                    "HOME": {"odds": format(Decimal(_raw["IOR_HRMH"]).normalize(), "f")},
                    "DRAW": {"odds": format(Decimal(_raw["IOR_HRMN"]).normalize(), "f")},
                    "AWAY": {"odds": format(Decimal(_raw["IOR_HRMC"]).normalize(), "f")},
                }
            except: pass
        # Half-time OU
        if "IOR_ROUHO" in _raw and "IOR_ROUHU" in _raw:
            try:
                match["markets"]["HTOU"] = {
                    "OVER": {"odds": format(Decimal(_raw["IOR_ROUHO"]).normalize(), "f")},
                    "UNDER": {"odds": format(Decimal(_raw["IOR_ROUHU"]).normalize(), "f")},
                    "line": _raw.get("RATIO_ROUHO", "") or _raw.get("RATIO_ROUHU", ""),
                }
            except: pass
        # Corner OU
        if "IOR_ROUCO" in _raw and "IOR_ROUCU" in _raw:
            try:
                match["markets"]["CNROU"] = {
                    "OVER": {"odds": format(Decimal(_raw["IOR_ROUCO"]).normalize(), "f")},
                    "UNDER": {"odds": format(Decimal(_raw["IOR_ROUCU"]).normalize(), "f")},
                    "line": _raw.get("RATIO_ROUCO", "") or _raw.get("RATIO_ROUCU", ""),
                }
            except: pass
        # Yes/No markets (score/shots/total)
        _yn_markets = [
            ("RSC", "SCORE"), ("RSH", "SHOTS"), ("RTS", "TOTS"),
        ]
        for _tag, _name in _yn_markets:
            _y_tag = f"IOR_{_tag}Y"; _n_tag = f"IOR_{_tag}N"
            if _y_tag in _raw and _n_tag in _raw:
                try:
                    match["markets"][_name] = {
                        "YES": {"odds": format(Decimal(_raw[_y_tag]).normalize(), "f")},
                        "NO": {"odds": format(Decimal(_raw[_n_tag]).normalize(), "f")},
                    }
                except: pass
        # Penalty 1X2
        if "IOR_RPXH" in _raw and "IOR_RPXN" in _raw:
            try:
                match["markets"]["PEN"] = {
                    "HOME": {"odds": format(Decimal(_raw["IOR_RPXH"]).normalize(), "f")},
                    "DRAW": {"odds": format(Decimal(_raw["IOR_RPXN"]).normalize(), "f")},
                    "AWAY": {"odds": format(Decimal(_raw["IOR_RPXC"]).normalize(), "f")},
                }
            except: pass
        # Race Even
        if "IOR_REH" in _raw and "IOR_REC" in _raw:
            try:
                match["markets"]["RE"] = {
                    "HOME": {"odds": format(Decimal(_raw["IOR_REH"]).normalize(), "f")},
                    "AWAY": {"odds": format(Decimal(_raw["IOR_REC"]).normalize(), "f")},
                    "line": _raw.get("RATIO_RE", ""),
                }
            except: pass


        # V5.2: build backward-compat "quotes" dict from 1X2 market
        if "1X2" in match.get("markets", {}):
            m1x2 = match["markets"]["1X2"]
            match["quotes"] = {
                "HOME": {"odds": m1x2["HOME"]["odds"]},
                "DRAW": {"odds": m1x2["DRAW"]["odds"]},
                "AWAY": {"odds": m1x2["AWAY"]["odds"]},
            }

        # Only emit if at least one market has valid odds
        if match["markets"]:
            out.append(match)

    return out


def _calc_rates(quotes: dict[str, dict[str, Any]]) -> tuple[str, str]:
    try:
        vals = [Decimal(quotes[c]["odds"]) for c in ("HOME", "DRAW", "AWAY")]
        s = sum(vals)
        rr = Decimal(1) / s
        margin = Decimal(1) - Decimal(1) / (Decimal(1)/vals[0] + Decimal(1)/vals[1] + Decimal(1)/vals[2])
        return (rr * 100).quantize(Decimal("0.01")), (margin * 100).quantize(Decimal("0.01"))
    except (KeyError, InvalidOperation, ValueError):
        return ("0", "0")

class HGDirectWorker:
    """Attach to a local Chrome CDP target and consume already-received data."""

    def __init__(self, cfg_provider, poll_seconds: float = 0.10) -> None:
        self.cfg_provider = cfg_provider
        self.poll_seconds = poll_seconds
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._feed: dict[str, Any] | None = None
        self._sig: dict[str, tuple] = {}
        self.last_event_at: str | None = None
        self.last_frame_ms: int | None = None
        self.frames_seen = 0
        self.frames_json = 0
        self.matches_seen = 0
        self.change_count = 0
        self.connect_count = 0
        self.transform_requests = 0
        self.transform_json = 0
        self.last_transform_url: str | None = None
        self.last_transform_status: int | None = None
        self.last_transform_at: str | None = None
        self.last_transform_decode: str | None = None
        self.last_error: str | None = None
        self.connected = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="HGDirectCDP", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self.connected = False

    def snapshot(self) -> dict[str, Any] | None:
        return self._feed

    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.cfg_provider()),
            "transport": "cdp-direct",
            "connected": self.connected,
            "cdp_url": (self.cfg_provider() or {}).get("cdp_url", "http://127.0.0.1:9222"),
            "page_url": (self.cfg_provider() or {}).get("page_url", ""),
            "use_proxy": False,
            "require_v2": False,
            "connect_count": self.connect_count,
            "transform_requests": self.transform_requests,
            "transform_json": self.transform_json,
            "last_transform_url": self.last_transform_url,
            "last_transform_status": self.last_transform_status,
            "last_transform_at": self.last_transform_at,
            "last_transform_decode": self.last_transform_decode,
            "frames_seen": self.frames_seen,
            "frames_json": self.frames_json,
            "matches_seen": self.matches_seen,
            "change_count": self.change_count,
            "last_event_at": self.last_event_at,
            "last_frame_ms": self.last_frame_ms,
            "last_error": self.last_error,
            "cache_matches": len((self._feed or {}).get("matches", [])),
            "cache_generated_at": (self._feed or {}).get("generated_at"),
        }

    def _run(self) -> None:
        if not self.cfg_provider():
            return
        while not self._stop.is_set():
            try:
                self._consume_target()
            except Exception as exc:
                self.connected = False
                self.last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(1.0)

    def _target(self) -> tuple[str, str]:
        cfg = self.cfg_provider() or {}
        base = cfg.get("cdp_url") or os.environ.get("HG_CDP_URL") or "http://127.0.0.1:9222"
        r = requests.get(base.rstrip("/") + "/json/list", timeout=3)
        r.raise_for_status()
        items = r.json()
        hosts = cfg.get("page_hosts") or _csv_env("HG_PAGE_HOSTS", "hga050.com,crw066.com")
        hosts = [str(h).lower().lstrip(".") for h in hosts if str(h).strip()]
        if not hosts:
            hosts = ["hga050.com", "crw066.com"]
        target = next((x for x in items if x.get("type") == "page" and any(
            (urlparse(x.get("url") or "").hostname or "").lower() == h or
            (urlparse(x.get("url") or "").hostname or "").lower().endswith("." + h)
            for h in hosts)), None)
        if not target or not target.get("webSocketDebuggerUrl"):
            raise RuntimeError("No CDP page target found. Start Chrome with remote debugging and open HG, then login manually.")
        return target["webSocketDebuggerUrl"], target.get("url", "")

    def _consume_target(self) -> None:
        ws_url, page_url = self._target()
        self.connect_count += 1
        ws = websocket.create_connection(ws_url, timeout=5, origin="http://127.0.0.1")
        response_meta: dict[str, dict[str, Any]] = {}
        request_meta: dict[str, dict[str, Any]] = {}
        ws_meta: dict[str, str] = {}
        try:
            self.connected = True
            self.last_error = None
            next_id = 1
            pending_body: dict[int, tuple[str, str, bool, int | None]] = {}
            cfg = self.cfg_provider() or {}

            def send(method: str, params: dict[str, Any] | None = None) -> int:
                nonlocal next_id
                mid = next_id
                next_id += 1
                ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                return mid

            send("Network.enable", {
                "maxTotalBufferSize": 32 * 1024 * 1024,
                "maxResourceBufferSize": 4 * 1024 * 1024,
            })
            send("Page.enable")

            # Walk the 早盘 league list: click each league so the authenticated
            # get_game_list XHR fires and its response is captured by the loop below.
            crawl_state = {"phase": "tab", "idx": 0, "expanded": 0, "league_clicked": 0}

            while not self._stop.is_set():
                started = time.time()
                ws.settimeout(max(0.2, self.poll_seconds + 0.2))
                try:
                    msg = json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                except websocket.WebSocketConnectionClosedException:
                    raise RuntimeError("CDP websocket closed")

                # ---- 早盘联赛爬取（阶段推进） ----
                # 每轮派发一个 Runtime.evaluate 动作，响应由下方网络管线自动入 feed。
                try:
                    _expr = self._crawl_expr(crawl_state)
                    if _expr:
                        send("Runtime.evaluate", {"expression": _expr, "returnByValue": True})
                except Exception:
                    pass
                self.last_frame_ms = round((time.time() - started) * 1000)
                self.frames_seen += 1

                mid = msg.get("id")
                if mid in pending_body:
                    source_url, req_type, is_transform, status_code = pending_body.pop(mid)
                    body_info = msg.get("result") or {}
                    body = body_info.get("body")
                    if body:
                        if body_info.get("base64Encoded"):
                            try:
                                body = base64.b64decode(body).decode("utf-8", "ignore")
                            except Exception:
                                body = ""
                        if is_transform:
                            self.last_transform_url = source_url
                            self.last_transform_status = status_code
                            self.last_transform_at = _now()
                            parsed = _parse_json_payload(body)
                            self.last_transform_decode = "json" if parsed is not None else "unparsed"
                            if parsed is not None:
                                self.transform_json += 1
                        self._ingest_payload(body, None, source_url)
                    continue

                method = msg.get("method", "")
                params = msg.get("params") or {}

                if method == "Network.webSocketFrameReceived":
                    resp = params.get("response") or {}
                    payload = resp.get("payloadData", "")
                    rid = params.get("requestId")
                    source = ws_meta.get(rid, page_url)
                    self._ingest_payload(payload, resp.get("opcode"), source)
                    continue

                if method == "Network.requestWillBeSent":
                    req = params.get("request") or {}
                    rid = params.get("requestId")
                    url = req.get("url") or page_url
                    if rid:
                        is_transform = _is_hg_transform_url(url, cfg)
                        request_meta[rid] = {"url": url, "method": req.get("method", "GET"), "is_transform": is_transform}
                        if is_transform:
                            self.transform_requests += 1
                            self.last_transform_url = url
                            self.last_transform_at = _now()
                    continue

                if method == "Network.webSocketCreated":
                    rid = params.get("requestId")
                    if rid:
                        ws_meta[rid] = params.get("url") or page_url
                    continue

                if method == "Network.responseReceived":
                    resp = params.get("response") or {}
                    typ = params.get("type", "")
                    mime = (resp.get("mimeType") or "").lower()
                    rid = params.get("requestId")
                    if not rid:
                        continue
                    url = resp.get("url") or (request_meta.get(rid) or {}).get("url") or page_url
                    is_transform = _is_hg_transform_url(url, cfg)
                    if typ not in ("XHR", "Fetch") and "json" not in mime and not is_transform:
                        continue
                    response_meta[rid] = {"url": url, "type": typ, "mime": mime, "is_transform": is_transform, "status": resp.get("status")}
                    if is_transform:
                        self.last_transform_url = url
                        try:
                            self.last_transform_status = int(resp.get("status"))
                        except (TypeError, ValueError):
                            self.last_transform_status = None
                        self.last_transform_at = _now()
                    continue

                if method == "Network.loadingFinished":
                    rid = params.get("requestId")
                    meta = response_meta.pop(rid, None)
                    if not meta:
                        continue
                    try:
                        req_id = send("Network.getResponseBody", {"requestId": rid})
                        pending_body[req_id] = (meta["url"], meta["type"], bool(meta.get("is_transform")), meta.get("status"))
                    except Exception:
                        pass
                    continue

        finally:
            self.connected = False
            try:
                ws.close()
            except Exception:
                pass

    def _crawl_expr(self, st: dict) -> str:
        # Click the 早盘 tab once so the page fires its bulk get_game_list (showtype=soon) load.
        # Then mark done — do NOT click individual leagues (that triggers HG 502 rate-limit).
        ph = st.get("phase")
        if ph == "tab":
            st["phase"] = "done"
            return "(function(){var el=document.getElementById('early_page');if(el){var c=new MouseEvent('click',{bubbles:true});el.dispatchEvent(c);return 'early_tab';}var a=document.getElementsByTagName('*');for(var i=0;i<a.length;i++){var e=a[i];var t=(e.textContent||'').trim();if(t==='早盘'&&(e.onclick||e.id)){var c2=new MouseEvent('click',{bubbles:true});e.dispatchEvent(c2);return 'early_text';}}return 'notab';})()"
        return ""
    def _ingest_payload(self, payload: Any, opcode: Any, source_url: str) -> None:
        self.last_event_at = _now()

        # HG transform.php returns XML (text/xml) for get_game_list etc.
        if isinstance(payload, str) and payload.lstrip().startswith(("<?xml", "<serverresponse")):
            matches = extract_hg_xml_matches(payload, source_url)
            if not matches:
                return
            self.frames_json += 1
            self._merge_matches(matches, source_url)
            return

        parsed = _parse_json_payload(payload)
        if parsed is None:
            return
        self.frames_json += 1
        matches = extract_matches(parsed, source_url)
        if not matches:
            return
        self._merge_matches(matches, source_url)

    def _merge_matches(self, matches: list[dict[str, Any]], source_url: str) -> None:
        self.matches_seen = max(self.matches_seen, len(matches))
        updated: dict[str, dict[str, Any]] = {}
        for m in matches:
            # Preserve ALL markets from XML extraction, not just 1X2
            q = m.get("quotes", {})
            rr, margin = _calc_rates(q) if q else ("0.0", "0.0")
            markets_raw = m.get("markets", {})
            extra = m.get("extra_odds", {})
            # Build enriched output: keep quotes (backward compat) + markets (multi-market)
            enriched = {
                "quotes": {
                    code: {**q[code], "quote_version": 0, "source_status": "OPEN", "source_timestamp": self.last_event_at}
                    for code in q
                } if q else {},
                "markets": markets_raw,   # ALL market types from XML: 1X2, AH, OU, EO, GL, HTOU, CNROU, TOTS, RE, etc.
                "extra_odds": extra,      # Raw IOR/RATIO tags for any unparsed markets
                "hg_return_rate_pct": str(rr),
                "hg_margin_pct": str(margin),
                "updated_at": self.last_event_at,
                "source": "HG",
            }
            m.update(enriched)
            updated[m["match_id"]] = m
        if not updated:
            return

        old = self._feed or {}
        old_map = {m["match_id"]: m for m in old.get("matches", [])}
        merged = dict(old_map)
        merged.update(updated)
        matches_sorted = sorted(merged.values(), key=lambda x: (str(x.get("kickoff_time", "")), x["match_id"]))
        self._feed = {
            "data_mode": "real",
            "source": "HG",
            "source_status": "CONNECTED",
            "transport": "cdp-direct",
            "generated_at": self.last_event_at,
            "match_count": len(matches_sorted),
            "market_note": "HG：仅展示从当前已登录浏览器会话实际收到并成功识别的 1X2 数据；未识别的数据不会伪造",
            "page_url": source_url,
            "matches": matches_sorted,
        }

        new_sig = {
            m["match_id"]: tuple(m["quotes"][c]["odds"] for c in ("HOME", "DRAW", "AWAY"))
            for m in matches_sorted
        }
        changed = []
        for mid, sig in new_sig.items():
            if self._sig.get(mid) != sig:
                changed.append({"match_id": mid, "kind": "odds" if mid in self._sig else "new", "new": sig})
        self._sig = new_sig
        if not changed:
            return
        self.change_count += len(changed)
        try:
            from backend import realtime
            realtime.bus.broadcast_threadsafe({
                "event": "hg_quotes_changed",
                "changed_matches": changed[:100],
                "source": "HG",
                "transport": "cdp-direct",
                "feed": self._feed,
                "worker": self.status(),
                "server_time": self.last_event_at,
            })
        except Exception:
            log.exception("Failed to broadcast HG update")


def make_cfg(base: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = dict(base or {})
    env_enabled = os.environ.get("HG_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
    cfg["enabled"] = bool(cfg.get("enabled", env_enabled)) and env_enabled
    cfg["transport"] = "cdp-direct"
    cfg["use_proxy"] = False
    cfg["require_v2"] = False
    cfg["cdp_url"] = cfg.get("cdp_url") or os.environ.get("HG_CDP_URL") or "http://127.0.0.1:9222"
    cfg["host"] = cfg.get("host") or (urlparse(cfg.get("page_url", "")).hostname or "hga050.com")
    cfg["page_url"] = cfg.get("page_url") or os.environ.get("HG_PAGE_URL") or "https://m829.crw066.com/"
    cfg["page_hosts"] = cfg.get("page_hosts") or _csv_env("HG_PAGE_HOSTS", "hga050.com,crw066.com")
    cfg["data_hosts"] = cfg.get("data_hosts") or _csv_env("HG_DATA_HOSTS", "crw066.com")
    cfg["transform_paths"] = cfg.get("transform_paths") or _csv_env("HG_TRANSFORM_PATHS", "/transform.php,/transform_nl.php")
    # Never persist secrets here.
    for k in ("username", "password", "cookie", "token", "authorization", "proxy"):
        cfg.pop(k, None)
    return cfg
