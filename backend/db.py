from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("FOOTBALL_DB", ROOT / "data" / "football.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = r"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS matches (
    id TEXT PRIMARY KEY,
    sport TEXT NOT NULL,
    competition TEXT,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    kickoff_time_utc TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PREMATCH',
    canonical_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_match_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    source_match_id TEXT NOT NULL,
    canonical_match_id TEXT NOT NULL,
    match_confidence REAL NOT NULL,
    mapping_status TEXT NOT NULL,
    source_home_name TEXT,
    source_away_name TEXT,
    source_kickoff_time TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(source, source_match_id)
);

CREATE TABLE IF NOT EXISTS markets (
    id TEXT PRIMARY KEY,
    canonical_match_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    line TEXT,
    status TEXT NOT NULL DEFAULT 'OPEN',
    settlement_rule_id TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS selections (
    id TEXT PRIMARY KEY,
    market_id TEXT NOT NULL,
    code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    settlement_outcome_code TEXT NOT NULL,
    UNIQUE(market_id, code)
);

CREATE TABLE IF NOT EXISTS raw_snapshots (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    collector_version TEXT NOT NULL,
    content_type TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    source_url TEXT,
    metadata_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quotes (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_quote_id TEXT,
    canonical_match_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    odds_decimal TEXT NOT NULL,
    source_status TEXT NOT NULL,
    source_timestamp TEXT NOT NULL,
    received_at TEXT NOT NULL,
    normalized_at TEXT NOT NULL,
    quote_version INTEGER NOT NULL,
    payload_hash TEXT NOT NULL,
    source_url TEXT,
    raw_snapshot_id TEXT,
    is_valid INTEGER NOT NULL DEFAULT 1,
    invalid_reason TEXT
);

CREATE TABLE IF NOT EXISTS calculation_snapshots (
    id TEXT PRIMARY KEY,
    canonical_match_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    tc_quote_version INTEGER,
    hg_quote_version INTEGER,
    match_version INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'CREATED'
);

CREATE TABLE IF NOT EXISTS calculation_snapshot_items (
    snapshot_id TEXT NOT NULL,
    quote_id TEXT NOT NULL,
    source TEXT NOT NULL,
    market_id TEXT NOT NULL,
    selection_id TEXT NOT NULL,
    quote_version INTEGER NOT NULL,
    odds_decimal TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, quote_id)
);

CREATE TABLE IF NOT EXISTS calculations (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    canonical_match_id TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL,
    calculation_version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    calc_finished_at TEXT,
    timechain_json TEXT
);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    canonical_match_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL,
    market_signature TEXT NOT NULL,
    status TEXT NOT NULL,
    classification TEXT NOT NULL,
    grade TEXT NOT NULL DEFAULT 'C',
    total_stake TEXT NOT NULL,
    min_pnl TEXT NOT NULL,
    max_pnl TEXT NOT NULL,
    roi TEXT NOT NULL,
    coverage_status TEXT NOT NULL,
    data_quality_status TEXT NOT NULL,
    execution_status TEXT NOT NULL,
    calculation_version INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    rule_version INTEGER NOT NULL,
    detected_at TEXT NOT NULL,
    expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_json TEXT NOT NULL,
    rebate_total TEXT,
    covered_states TEXT,
    uncovered_states TEXT
);

CREATE TABLE IF NOT EXISTS quote_events (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    canonical_match_id TEXT NOT NULL,
    market_id TEXT NOT NULL,
    selection_id TEXT,
    event_type TEXT NOT NULL,
    old_odds TEXT,
    new_odds TEXT,
    quote_version INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- P0.8 证据层：每一次真实抓取全量保存（无论赔率是否变化）
CREATE TABLE IF NOT EXISTS source_observations (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_match_id TEXT,
    source_market_id TEXT,
    source_selection_id TEXT,
    odds TEXT,
    line TEXT,
    market_type TEXT,
    source_status TEXT,
    source_timestamp TEXT,
    collected_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    raw_snapshot_id TEXT,
    payload_hash TEXT,
    collector_version TEXT,
    quote_id TEXT,
    quote_version INTEGER,
    canonical_match_id TEXT,
    market_id TEXT,
    selection_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_lookup ON source_observations(source, market_id, selection_id, received_at DESC);

-- P0.8 机会版本化：OPP-1001 的 V1/V2/V3...（REPLACED 链）
CREATE TABLE IF NOT EXISTS opportunity_versions (
    id TEXT PRIMARY KEY,
    opportunity_group TEXT NOT NULL,
    version INTEGER NOT NULL,
    canonical_match_id TEXT NOT NULL,
    status TEXT NOT NULL,
    classification TEXT NOT NULL,
    grade TEXT,
    total_stake TEXT,
    min_pnl TEXT,
    max_pnl TEXT,
    roi TEXT,
    coverage_status TEXT,
    sum_inv TEXT,
    rule_version INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    calculation_version INTEGER NOT NULL,
    quote_versions_json TEXT NOT NULL,
    prev_version_id TEXT,
    replaced_by TEXT,
    result_json TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(opportunity_group, version)
);
CREATE INDEX IF NOT EXISTS idx_oppver_match ON opportunity_versions(canonical_match_id, created_at DESC);

-- P0.8 水位迭代：哪个赔率变化导致套利 Pmin 从 +12 变 +3
CREATE TABLE IF NOT EXISTS water_iterations (
    id TEXT PRIMARY KEY,
    match_id TEXT NOT NULL,
    canonical_market_id TEXT,
    from_quote_version INTEGER,
    to_quote_version INTEGER,
    changed_selection TEXT,
    changed_source TEXT,
    old_odds TEXT,
    new_odds TEXT,
    changed_at TEXT NOT NULL,
    old_opportunity_version_id TEXT,
    new_opportunity_version_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_water_match ON water_iterations(match_id, created_at DESC);

-- P0.8 能力矩阵：FULLTIME_1X2 × TC=SUPPORTED / HG=SUPPORTED / OU=NOT_P0
CREATE TABLE IF NOT EXISTS market_capabilities (
    id TEXT PRIMARY KEY,
    canonical_market TEXT NOT NULL,
    source TEXT NOT NULL,
    support_status TEXT NOT NULL,
    settlement_rule TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    note TEXT,
    UNIQUE(canonical_market, source, version)
);

-- P0.8 源健康：每个来源 last_success/latency/failure_streak
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY,
    last_success_at TEXT,
    last_failure_at TEXT,
    failure_streak INTEGER NOT NULL DEFAULT 0,
    last_observation_at TEXT,
    last_quote_at TEXT,
    collector_latency_ms REAL,
    last_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    updated_at TEXT NOT NULL
);

-- P0.8 #20 投注原子 / 门票正式落表：机会 → 具体投注（可追溯、可对账）
CREATE TABLE IF NOT EXISTS bet_atoms (
    id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    opportunity_version_id TEXT,
    match_id TEXT NOT NULL,
    source TEXT NOT NULL,
    market_type TEXT NOT NULL,
    line TEXT,
    selection_code TEXT NOT NULL,
    odds TEXT NOT NULL,
    stake TEXT NOT NULL,
    rebate_rate TEXT NOT NULL,
    settlement_rule TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_atoms_plan ON bet_atoms(plan_id);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    opportunity_version_id TEXT,
    status TEXT NOT NULL,
    total_stake TEXT NOT NULL,
    worst_pnl TEXT NOT NULL,
    worst_roi TEXT,
    rebate_total TEXT,
    budget TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickets_plan ON tickets(plan_id);

CREATE TABLE IF NOT EXISTS bulletins (
    bulletin_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    sale_open_time TEXT,
    sale_close_time TEXT,
    invalid_match_num TEXT,
    start_time TEXT,
    end_time TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quotes_match_market ON quotes(canonical_match_id, market_id, selection_id, quote_version DESC);
CREATE INDEX IF NOT EXISTS idx_opps_match ON opportunities(canonical_match_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_qevents_match ON quote_events(canonical_match_id, received_at DESC);

-- P0.8 HG independent live state (current open 1X2 per GID, upserted)
CREATE TABLE IF NOT EXISTS hg_live_matches (
    gid TEXT PRIMARY KEY,
    competition TEXT,
    home TEXT,
    away TEXT,
    kickoff_time TEXT,
    score_h TEXT,
    score_c TEXT,
    home_odds TEXT,
    draw_odds TEXT,
    away_odds TEXT,
    return_rate_pct TEXT,
    margin_pct TEXT,
    source TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hg_live_updated ON hg_live_matches(updated_at DESC);
"""

# 对已有 DB 的增量迁移：缺列就 ALTER TABLE ADD COLUMN
_MIGRATIONS = [
    ("calculations", "calc_finished_at", "TEXT"),
    ("calculations", "timechain_json", "TEXT"),
    ("calculations", "calc_signature", "TEXT"),
    ("opportunities", "rebate_total", "TEXT"),
    ("opportunities", "covered_states", "TEXT"),
    ("opportunities", "uncovered_states", "TEXT"),
    ("opportunities", "grade", "TEXT DEFAULT 'C'"),
    ("matches", "status", "TEXT DEFAULT 'SCHEDULED'"),
    ("matches", "status_updated_at", "TEXT"),
    # P0.8 串关版本化：方案版本链（同 plan_id 递增）+ 报价签名（无变化不重写）+ 水位追溯
    ("tickets", "version", "INTEGER DEFAULT 1"),
    ("tickets", "signature", "TEXT"),
    ("tickets", "quote_versions_json", "TEXT"),
    ("tickets", "superseded_at", "TEXT"),
]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # WAL：scheduler 高频写 + 前端高频读并发不互锁（修复页面卡顿根因）
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(SCHEMA)
        for table, column, coltype in _MIGRATIONS:
            existing = _table_columns(conn, table)
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
        # quote_events.selection_id 语义修正：机会生命周期事件无 selection → 允许 NULL
        qe_cols = {r[1]: r for r in conn.execute("PRAGMA table_info(quote_events)")}
        if "selection_id" in qe_cols and qe_cols["selection_id"][3] == 1:
            conn.execute("ALTER TABLE quote_events RENAME TO quote_events_old")
            conn.execute(
                "CREATE TABLE quote_events ("
                "id TEXT PRIMARY KEY, source TEXT NOT NULL, canonical_match_id TEXT NOT NULL, "
                "market_id TEXT NOT NULL, selection_id TEXT, event_type TEXT NOT NULL, "
                "old_odds TEXT, new_odds TEXT, quote_version INTEGER NOT NULL, "
                "received_at TEXT NOT NULL, created_at TEXT NOT NULL)")
            conn.execute("INSERT INTO quote_events SELECT * FROM quote_events_old")
            conn.execute("DROP TABLE quote_events_old")
        conn.commit()


# P0.8 能力矩阵（#10）：哪些 canonical 市场 × 源 = 支持/不支持
# DAFABET 定位 = 第三参考源（HG 未接入前暂代对手源参与计算）
_CAPABILITY_SEED = [
    # (canonical_market, source, support_status, settlement_rule, note)
    ("FULLTIME_1X2", "TC", "SUPPORTED", "1x2_v1", "P0 主源"),
    ("FULLTIME_1X2", "HG", "SUPPORTED", "1x2_v1", "P0 对手源（未接入，接口待建）"),
    ("FULLTIME_1X2", "DAFABET", "SUPPORTED", "1x2_v1", "第三参考源·暂代对手源参与计算"),
    ("FULLTIME_1X2", "ODDSAPI", "SUPPORTED", "1x2_v1", "Odds API 对手源·best price h2h"),
    ("ASIAN_HANDICAP", "TC", "NOT_P0", "ah_v1", "P0 不做 TC 让球"),
    ("ASIAN_HANDICAP", "HG", "SUPPORTED", "ah_v1", "P0 对手源候选玩法"),
    ("ASIAN_HANDICAP", "DAFABET", "SUPPORTED", "ah_v1", "参考源五态结算"),
    ("OVER_UNDER", "TC", "NOT_P0", "ou_v1", "P0 不做 TC 大小球"),
    ("OVER_UNDER", "HG", "NOT_P0", "ou_v1", "P0 不做"),
    ("OVER_UNDER", "DAFABET", "SUPPORTED", "ou_v1", "参考源·P0 仅数据就绪，不参与对冲"),
]


def seed_capabilities() -> None:
    """幂等写入能力矩阵（INSERT OR IGNORE）。"""
    ts = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with sqlite3.connect(DB_PATH) as conn:
        for cm, source, status, rule, note in _CAPABILITY_SEED:
            conn.execute(
                """INSERT OR IGNORE INTO market_capabilities
                   (id,canonical_market,source,support_status,settlement_rule,version,
                    effective_from,effective_to,note)
                   VALUES (?,?,?,?,?,1,?,NULL,?)""",
                (f"CAP_{cm}_{source}_1", cm, source, status, rule, ts, note))
        conn.commit()


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetchall(sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def fetchone(sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def execute(sql: str, params: tuple[Any, ...] = ()) -> None:
    with db() as conn:
        conn.execute(sql, params)


def row_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
