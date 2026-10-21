"""HG → canonical unified-model adapter (V2-3).

Maps live HG 1X2 quotes into the canonical matches/markets/quotes model so the
existing engine (authoritative_calculate) can compute cross-source results.

Rules (no fuzzy matching into executable results):
  * Match key = exact normalized (home, away) against canonical SCHEDULED matches.
  * When both kickoffs parse, require the same calendar date (+08).
  * Skip HG matches that are in-play (running=True), stale (updated_at older
    than STALE_AFTER_SEC), or lack a valid 1X2 quote set.
  * Unmatched HG matches stay in hg_live_matches only — never written to the
    canonical quotes table, so no fake cross-source opportunity is produced.
"""
from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from backend.collector.collector import upsert_normalized_quote
from backend.db import fetchall

STALE_AFTER_SEC = 120.0  # HG quote older than this must not enter calculation


def _norm_team_name(v: Any) -> str:
    s = str(v or "").lower().strip()
    aliases = {
        "man utd": "manchester united",
        "man united": "manchester united",
        "manchester utd": "manchester united",
        "tottenham": "tottenham hotspur",
        "spurs": "tottenham hotspur",
        "psg": "paris saint-germain",
    }
    s = aliases.get(s, s)
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", s)


_HG_KICKOFF_RE = re.compile(r"^(\d{1,2})-(\d{1,2})[\s]+(\d{1,2}):(\d{2})([ap])$")


def _hg_kickoff_iso(kickoff: str, year: int | None = None) -> str | None:
    """'10-08 08:30p' (site local +08) → ISO UTC. Returns None if unparseable."""
    m = _HG_KICKOFF_RE.match((kickoff or "").strip())
    if not m:
        return None
    month, day, hour, minute, ap = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4)), m.group(5)
    if ap == "p" and hour < 12:
        hour += 12
    if ap == "a" and hour == 12:
        hour = 0
    y = year or datetime.now(timezone.utc).year
    try:
        local = datetime(y, month, day, hour, minute)  # naive wall time in +08
    except ValueError:
        return None
    # wall(+08) -> UTC: subtract 8h, then stamp UTC
    return (local - timedelta(hours=8)).replace(tzinfo=timezone.utc).isoformat()


def _same_date(a: str, b: str) -> bool:
    """Both ISO strings → same calendar date in +08."""
    try:
        da = datetime.fromisoformat(a).astimezone(timezone.utc) + timedelta(hours=8)
        db = datetime.fromisoformat(b).astimezone(timezone.utc) + timedelta(hours=8)
        return (da.year, da.month, da.day) == (db.year, db.month, db.day)
    except Exception:
        return False


def _valid_1x2(m: dict) -> bool:
    q = m.get("quotes") or {}
    try:
        vals = [Decimal(q[c]["odds"]) for c in ("HOME", "DRAW", "AWAY")]
        return all(v > 1 for v in vals)
    except Exception:
        return False


def sync_hg_to_canonical(hg_matches: list[dict], source_url: str = "") -> dict[str, Any]:
    """Write matched, fresh, pre-match HG 1X2 quotes into the canonical model.

    Returns a report {matched, skipped_stale, skipped_running, skipped_kickoff,
    skipped_no_quote, inserted_quotes, unchanged}.
    """
    now = time.time()
    report = {
        "matched": 0, "skipped_stale": 0, "skipped_running": 0,
        "skipped_kickoff": 0, "skipped_no_quote": 0,
        "inserted_quotes": 0, "unchanged": 0, "unmatched": 0,
    }
    if not hg_matches:
        return report

    # Canonical SCHEDULED matches (engine only computes these)
    rows = fetchall(
        """SELECT id, home_team, away_team, kickoff_time_utc FROM matches
           WHERE status='SCHEDULED' AND kickoff_time_utc > ?""",
        (datetime.now(timezone.utc).isoformat(),),
    )
    canon_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        key = (_norm_team_name(r["home_team"]), _norm_team_name(r["away_team"]))
        if key[0] and key[1]:
            canon_by_key[key] = r

    ts = datetime.now(timezone.utc).isoformat()
    for hm in hg_matches:
        # freshness
        try:
            ut = datetime.fromisoformat(hm.get("updated_at", "")).timestamp()
        except (ValueError, TypeError):
            ut = 0.0
        if (now - ut) > STALE_AFTER_SEC:
            report["skipped_stale"] += 1
            continue
        # in-play guard
        if hm.get("running"):
            report["skipped_running"] += 1
            continue
        if not _valid_1x2(hm):
            report["skipped_no_quote"] += 1
            continue
        key = (_norm_team_name(hm.get("home")), _norm_team_name(hm.get("away")))
        canon = canon_by_key.get(key)
        if not canon:
            report["unmatched"] += 1
            continue
        # kickoff date check (only when HG provides one)
        hg_kick = _hg_kickoff_iso(hm.get("kickoff_time", ""))
        if hg_kick is not None and not _same_date(hg_kick, canon["kickoff_time_utc"]):
            report["skipped_kickoff"] += 1
            continue

        # Build normalized payload and reuse the proven persistence path.
        n = {
            "source": "HG",
            "source_match_id": str(hm.get("match_id", "")),
            "competition": hm.get("competition") or canon.get("competition") or "",
            "home_team": hm.get("home") or canon["home_team"],
            "away_team": hm.get("away") or canon["away_team"],
            "kickoff_time_utc": canon["kickoff_time_utc"],
            "match_id": canon["id"],
            "market_id": "MK_" + hashlib.sha1(f"{canon['id']}|MATCH_RESULT|None".encode()).hexdigest()[:16],
            "market_type": "MATCH_RESULT",
            "line": None,
            "selections": [
                {"code": "HOME", "display_name": "主胜", "odds": hm["quotes"]["HOME"]["odds"], "status": "OPEN"},
                {"code": "DRAW", "display_name": "平局", "odds": hm["quotes"]["DRAW"]["odds"], "status": "OPEN"},
                {"code": "AWAY", "display_name": "客胜", "odds": hm["quotes"]["AWAY"]["odds"], "status": "OPEN"},
            ],
            "source_timestamp": hm.get("updated_at") or ts,
            "source_url": source_url,
        }
        before = _count_hg_quotes(canon["id"])
        upsert_normalized_quote(n, raw_snapshot_id=None)
        after = _count_hg_quotes(canon["id"])
        report["matched"] += 1
        if after > before:
            report["inserted_quotes"] += after - before
        else:
            report["unchanged"] += 1
    return report


def _count_hg_quotes(canonical_match_id: str) -> int:
    from backend.db import fetchone
    r = fetchone(
        """SELECT COUNT(*) AS c FROM quotes WHERE source='HG' AND canonical_match_id=?""",
        (canonical_match_id,),
    )
    return int(r["c"]) if r else 0
