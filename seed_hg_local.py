"""
Aggregate ALL available HG XML snapshots into one comprehensive seed feed.
Includes: all runtime/*.xml with <original> JSON, deduplicated by GID,
plus DB cache matches for 1X2 coverage.
"""
import json, sys, os, xml.etree.ElementTree as ET
from datetime import datetime, timezone

sys.path.insert(0, r"C:\1\football_hedge_p0\football_hedge_p0")
from backend.hg_direct import extract_hg_xml_matches, _calc_rates

def main():
    all_matches = {}  # GID -> match dict (prefer richer data)
    xml_files = [
        r"C:\1\football_hedge_p0\football_hedge_p0\runtime\hg_baseline_(ltype=3).xml",
        r"C:\1\football_hedge_p0\football_hedge_p0\runtime\hg_ltype=1.xml",
        r"C:\1\football_hedge_p0\football_hedge_p0\runtime\hg_ltype=2.xml",
        r"C:\1\football_hedge_p0\football_hedge_p0\runtime\hg_ltype=4.xml",
        r"C:\1\football_hedge_p0\football_hedge_p0\hg_raw_now.xml",
        r"C:\1\football_hedge_p0\football_hedge_p0\_hg_game_list_sample.xml",
    ]
    
    for fpath in xml_files:
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw_xml = f.read()
            matches = extract_hg_xml_matches(raw_xml, f"file://{os.path.basename(fpath)}")
            for m in matches:
                gid = m.get("match_id", "")
                if not gid:
                    continue
                # Prefer matches with more markets
                existing = all_matches.get(gid)
                if existing:
                    if len(m.get("markets", {})) > len(existing.get("markets", {})):
                        all_matches[gid] = m
                else:
                    all_matches[gid] = m
            print(f"  {os.path.basename(fpath)}: {len(matches)} parsed, total unique: {len(all_matches)}")
        except Exception as e:
            print(f"  {os.path.basename(fpath)}: ERROR - {e}")
    
    # Add DB cache matches (1X2 only, but many more matches)
    try:
        import sqlite3
        db_path = r"C:\1\football_hedge_p0\football_hedge_p0\data\football.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT gid, competition, home, away, kickoff_time, score_h, score_c,"
            " home_odds, draw_odds, away_odds, return_rate_pct, margin_pct, updated_at"
            " FROM hg_live_matches ORDER BY updated_at DESC"
        ).fetchall()
        conn.close()
        
        db_added = 0
        for r in rows:
            gid = str(r["gid"])
            if gid in all_matches:
                continue
            if not all([r["home_odds"], r["draw_odds"], r["away_odds"]]):
                continue
            all_matches[gid] = {
                "match_id": gid,
                "competition": r["competition"] or "",
                "home": r["home"] or "",
                "away": r["away"] or "",
                "kickoff_time": r["kickoff_time"] or "",
                "score_h": str(r["score_h"] or ""),
                "score_c": str(r["score_c"] or ""),
                "quotes": {
                    "HOME": {"odds": str(r["home_odds"])},
                    "DRAW": {"odds": str(r["draw_odds"])},
                    "AWAY": {"odds": str(r["away_odds"])},
                },
                "markets": {
                    "1X2": {
                        "HOME": {"odds": str(r["home_odds"])},
                        "DRAW": {"odds": str(r["draw_odds"])},
                        "AWAY": {"odds": str(r["away_odds"])},
                    }
                },
                "hg_return_rate_pct": str(r["return_rate_pct"] or ""),
                "hg_margin_pct": str(r["margin_pct"] or ""),
                "updated_at": r["updated_at"] or "",
                "source": "HG",
                "cache": True,
                "stale": True,
            }
            db_added += 1
        print(f"  DB cache: {db_added} new matches (1X2 only), total unique: {len(all_matches)}")
    except Exception as e:
        print(f"  DB cache: ERROR - {e}")
    
    now = datetime.now(timezone.utc).isoformat()
    
    # Build feed
    matches_out = []
    market_types_seen = set()
    for gid, m in all_matches.items():
        # Calculate rates if missing
        q = m.get("quotes", {})
        if q and all(c in q for c in ("HOME", "DRAW", "AWAY")):
            try:
                rr, margin = _calc_rates(q)
                m["hg_return_rate_pct"] = str(rr)
                m["hg_margin_pct"] = str(margin)
            except:
                pass
        m["updated_at"] = m.get("updated_at", now)
        m["source"] = "HG"
        
        for mk_type in m.get("markets", {}):
            market_types_seen.add(mk_type)
        
        matches_out.append(m)
    
    matches_out.sort(key=lambda x: (x.get("kickoff_time", ""), x.get("match_id", "")))
    
    feed = {
        "data_mode": "real",
        "source": "HG",
        "source_status": "SEEDED",
        "transport": "aggregated-seed",
        "generated_at": now,
        "match_count": len(matches_out),
        "market_note": f"聚合 {len(xml_files)} 个 XML 快照 + DB 缓存，共 {len(market_types_seen)} 种玩法：{', '.join(sorted(market_types_seen))}",
        "matches": matches_out,
    }
    
    out_path = r"C:\1\football_hedge_p0\football_hedge_p0\data\hg_seed_feed.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, default=str)
    
    print(f"\n=== WRITTEN: {out_path} ===")
    print(f"Total matches: {len(matches_out)}")
    print(f"Market types: {sorted(market_types_seen)}")
    
    # Print summary of first 3 matches
    for m in matches_out[:3]:
        mks = list(m.get("markets", {}).keys())
        print(f"  {m.get('home','?')} vs {m.get('away','?')} | markets: {mks}")

if __name__ == "__main__":
    main()
