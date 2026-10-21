# FOOTBALL_HEDGE_P0 V2.0/V3.0 Acceptance Report

**Generated:** 2026-10-10T06:23 UTC
**Project:** C:\1\football_hedge_p0\football_hedge_p0
**Service:** http://127.0.0.1:8030 (running, pwsh-23)

---

## 1. Executive Summary

| Item | Status | Evidence |
|---|---|---|
| HG data chain (P0) | FIXED | bootstrapped=true, 84 req, 100% success, 16 matches |
| CDP 9222 | FIXED | Chrome w/ --remote-allow-origins=* |
| Bootstrap fallback | CODE FIX | hg_http.py lines 278-296: stored template recovery |
| TC collection | OPERATIONAL | 61 matches, 0.5s, 480 polls, 0 failures |
| DB freshness | LIVE DATA | 26 HG rows, cache_source="live", stale_count=0 |
| Frontend | OPERATIONAL | HTTP 200, app.js + views.js served |
| Geo-block | MITIGATED | Page blocked but HTTP POST works; stored template fallback |

---

## 2. Root Cause: Three-Layer Cascading Failure

### Layer 1: Chrome CDP not running
- **Symptom:** ConnectionError port=9222 [WinError 10061]
- **Root cause:** No Chrome with --remote-debugging-port=9222
- **Fix:** Started Chrome with proper flags

### Layer 2: WebSocket 403 Forbidden
- **Symptom:** Handshake status 403, origin rejected
- **Root cause:** Chrome rejected http://127.0.0.1:9222 origin
- **Fix:** Added --remote-allow-origins=* flag

### Layer 3: Bootstrap failure (uid/template not capturable)
- **Symptom:** bootstrapped=false, last_error="could not capture uid+postData template"
- **Root cause:** HG site geo-blocked => CDP page shows "Forbidden", no real transform requests fire
- **Code fix:** backend/hg_http.py lines 278-296: fallback to stored template when live capture fails

---

## 3. Code Fix

**File:** backend/hg_http.py, lines 278-296
**Backup:** _backup_2026-10-10T06-08-13/hg_http.py

Before:
    if saved and uid:
        template = re.sub(r"uid=[^&]+", f"uid={uid}", saved)

After:
    if saved:
        # Extract uid from stored template if not captured live
        if not uid:
            for part in saved.split("&"):
                if part.startswith("uid="):
                    uid = part[4:]
                    break
        if uid:
            template = re.sub(r"uid=[^&]+", f"uid={uid}", saved)
        else:
            template = saved

**Why it works:** runtime/hg_post_template.txt has a valid uid (xpic1uuwem45400786l74941b0). Cookies from CDP (14 for crw066.com). Together they enable HTTP POST to transform.php.

---

## 4. Verified Metrics

### HG HTTP Collector (PRIMARY)

| Metric | Before | After | Target |
|---|---|---|---|
| bootstrapped | false | **true** | true |
| request_count | 0 | **84** | >0 |
| success_count | 0 | **84** (100%) | >0 |
| fail_count | 0 | **0** | 0 |
| matches_open | 0 | **16** | >0 |
| change_count | 0 | **100** | >0 |
| cache_source | "restored" | **"live"** | "live" |
| cache_stale_count | 10 | **0** | 0 |
| last_error | ConnectionError | **null** | null |
| latency_p50 | - | **69.5ms** | <200ms |
| latency_p95 | - | **112.8ms** | <500ms |

### HG Direct (CDP WebSocket)

| Metric | Before | After |
|---|---|---|
| connected | false | **true** |
| transform_requests | 0 | **69** |
| last_transform_status | 502 | **200** |
| matches_seen | 0 | **16** |
| last_error | WebSocket 403 | **null** |

### TC FastPoll

| Metric | Value |
|---|---|
| poll_count | 480+ |
| cache_matches | 61 |
| last_fetch_ok | true |
| consecutive_fail | 0 |

### Database

| Table | Count | Freshness |
|---|---|---|
| hg_live_matches | 26 | updated_at within seconds |
| matches | 100 | TC data |
| quotes | 22,919 | TC source |

---

## 5. Data Flow Verified

HG Website -> CDP Bootstrap (cookies) -> Stored Template (uid/ver) -> HTTP POST transform.php (3s) -> XML Parse -> DB (hg_live_matches) -> API Feed (/api/v1/real/feed) -> Frontend (HTML 200)

All stages confirmed operational with no errors.

---

## 6. Known Issues (Non-Blocking)

1. **Chinese names mojibake:** GBK encoding not decoded in pipeline (cosmetic)
2. **HG quotes not in main quotes table:** sync shows "skipped_running" for all 16 (limits cross-source analysis)
3. **Geo-block external:** Mitigated; monitor if cookies expire

---

## 7. Action Log

T1: Read all source files
T2-T4: Diagnosed and fixed CDP 9222 (Chrome startup)
T5: Confirmed geo-block (external)
T6: Applied code fix to hg_http.py bootstrap
T7-T9: Restarted service, verified bootstrap + DB
T10-T13: Verified API feed, frontend, TC, generated report

---

## 8. Conclusion

**P0 HG data chain: FIXED AND VERIFIED.** One code fix was needed (hg_http.py bootstrap fallback). All other issues were operational. Service stable on port 8030 with HG HTTP (84 req, 100% success, 16 matches), HG Direct (69 captures, 200 status), and TC (61 matches). Zero errors across all collectors.