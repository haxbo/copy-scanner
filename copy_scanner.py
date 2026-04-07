"""
HaxFish Copy Scanner — Entry-copy target wallets' Polymarket positions.

Monitors one or more wallet addresses on Polymarket. When a target opens
a NEW position, we copy it once (entry-copy only — no exit mirroring,
no partial-exit, no top-up).

State separation:
  - copy_positions: observed target wallet positions (read-only mirror)
  - copy_trades:    OUR actual executed trades (source of truth for P&L)

Usage:
    python copy_scanner.py add 0xABC...            # add wallet to watch
    python copy_scanner.py remove 0xABC...         # stop watching
    python copy_scanner.py wallets                 # list watched wallets
    python copy_scanner.py sync                    # pull positions for all wallets
    python copy_scanner.py sync 0xABC...           # pull for one wallet
    python copy_scanner.py monitor                 # poll & copy (all enabled wallets)
    python copy_scanner.py monitor --dry-run       # show only, don't trade
    python copy_scanner.py status                  # show wallet positions + our trades
    python copy_scanner.py health                  # operational health snapshot
    python copy_scanner.py trades                  # show open/pending/closed trades
    python copy_scanner.py config                  # show config + runtime state
    python copy_scanner.py pause                   # pause trading (monitoring continues)
    python copy_scanner.py resume                  # resume trading + reset circuit breaker
    python copy_scanner.py cleanup                 # clean stale state + reset circuit breaker
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

from polymarket_scanner import (
    EXECUTOR_URL,
    EXECUTOR_API_KEY,
    clob_get_midpoint,
)
from Backend.order_manager import OrderManager

LOG_PATH = Path(__file__).parent / "logs" / "copy_scanner.log"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_PATH),
    ],
)
log = logging.getLogger("copy_scanner")

# ── Config ──────────────────────────────────────────────────────────────────

DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLY_GAMMA = "https://gamma-api.polymarket.com"

SETTINGS_PATH = Path(__file__).parent / "settings.json"
COPY_DB_PATH = Path(__file__).parent / "copy_scanner.db"

_order_manager = OrderManager()


class Config:
    """Immutable config snapshot loaded from settings.json."""
    __slots__ = (
        "max_shares", "max_positions", "tp_pct",
        "poll_interval", "max_price_slip",
        "max_daily_loss", "max_trades_per_day",
        "max_stake_per_wallet", "max_stake_per_slug",
        "max_total_exposure",
    )

    def __init__(self):
        s = json.loads(SETTINGS_PATH.read_text())

        # Max shares per trade. Polymarket minimum order is usually 5 shares.
        self.max_shares = int(s["copy_max_shares"])

        # Max open trades (pending + open) at any time.
        self.max_positions = int(s["copy_max_positions"])

        # Take-profit percentage. None = no auto TP sell.
        tp = s.get("copy_tp_pct")
        self.tp_pct = float(tp) if tp is not None else None

        # Seconds between each monitor scan cycle.
        self.poll_interval = int(s["copy_poll_interval"])

        # Max allowed price slippage between source entry price and current
        # midpoint. Relative for prices >= 0.10, absolute for lower prices.
        self.max_price_slip = float(s["copy_max_price_slip"])

        # Risk kill-switches — log if using defaults (keys missing from settings.json)
        _defaults = {
            "copy_max_daily_loss": 50,
            "copy_max_trades_per_day": 50,
            "copy_max_stake_per_wallet": 25,
            "copy_max_stake_per_slug": 10,
            "copy_max_total_exposure_usd": 100,
        }
        for key, default in _defaults.items():
            if key not in s:
                log.warning(f"Config '{key}' missing from settings.json, using default: {default}")

        # Daily loss cap (USD). Blocks new buys when realised P&L for the
        # current UTC day exceeds this. Unrealised losses are NOT counted —
        # only closed/resolved trades.
        self.max_daily_loss = float(s.get("copy_max_daily_loss", 50))

        # Max new trades per UTC day. Hard cap to prevent runaway activity
        # if a source wallet goes wild or detection logic misfires.
        self.max_trades_per_day = int(s.get("copy_max_trades_per_day", 50))

        # Max total open stake (USD) from any single source wallet.
        # Prevents concentration risk if one watched wallet is compromised.
        # Checked post-trade: current_exposure + proposed_stake > cap = skip.
        self.max_stake_per_wallet = float(s.get("copy_max_stake_per_wallet", 25))

        # Max total open stake (USD) on any single market slug.
        # Prevents correlated risk on the same event.
        # Checked post-trade: current_exposure + proposed_stake > cap = skip.
        self.max_stake_per_slug = float(s.get("copy_max_stake_per_slug", 10))

        # Global exposure cap (USD). Total open stake across ALL trades.
        # Prevents runaway capital deployment regardless of per-wallet/slug limits.
        # Checked post-trade: total_exposure + proposed_stake > cap = skip.
        self.max_total_exposure = float(s.get("copy_max_total_exposure_usd", 100))


# ── Database ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 3  # bump when schema changes

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS copy_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL UNIQUE,
    pseudonym TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    added_at TEXT DEFAULT (strftime('%Y-%m-%d %H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS copy_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL REFERENCES copy_wallets(id),
    asset TEXT NOT NULL,
    condition_id TEXT DEFAULT '',
    size REAL DEFAULT 0,
    avg_price REAL DEFAULT 0,
    initial_value REAL DEFAULT 0,
    current_value REAL DEFAULT 0,
    cash_pnl REAL DEFAULT 0,
    percent_pnl REAL DEFAULT 0,
    total_bought REAL DEFAULT 0,
    cur_price REAL DEFAULT 0,
    redeemable INTEGER DEFAULT 0,
    title TEXT DEFAULT '',
    slug TEXT DEFAULT '',
    icon TEXT DEFAULT '',
    event_slug TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    outcome_index INTEGER DEFAULT 0,
    opposite_outcome TEXT DEFAULT '',
    opposite_asset TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    negative_risk INTEGER DEFAULT 0,
    first_seen_at TEXT,
    last_seen_at TEXT,
    closed_at TEXT
);

-- Our actual executed trades — source of truth for what WE bought
CREATE TABLE IF NOT EXISTS copy_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    asset TEXT NOT NULL,
    slug TEXT DEFAULT '',
    title TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    source_wallet TEXT DEFAULT '',
    source_pseudonym TEXT DEFAULT '',
    our_entry_price REAL,
    our_stake REAL,
    our_size REAL,
    cur_price REAL,
    pnl_pct REAL DEFAULT 0,
    pnl_usd REAL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    placed_at TEXT NOT NULL,
    closed_at TEXT,
    resolved_price REAL
);

-- Skip log for analysis
CREATE TABLE IF NOT EXISTS copy_skips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skipped_at TEXT NOT NULL,
    asset TEXT,
    slug TEXT,
    title TEXT,
    outcome TEXT,
    source_pseudonym TEXT,
    reason TEXT,
    details TEXT
);

CREATE TABLE IF NOT EXISTS copy_schema_version (
    version INTEGER NOT NULL
);

-- Runtime state: circuit breaker, pause flag, etc. Persists across restarts.
CREATE TABLE IF NOT EXISTS copy_runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT
);

-- Order audit: every buy attempt recorded for debugging & post-mortems.
CREATE TABLE IF NOT EXISTS copy_order_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER,
    asset TEXT,
    slug TEXT,
    source_wallet TEXT,
    source_pseudonym TEXT,
    attempt_time TEXT,
    requested_price REAL,
    requested_stake REAL,
    requested_size REAL,
    result_status TEXT,
    error_message TEXT,
    retry_count INTEGER DEFAULT 0,
    order_id TEXT
);
"""

_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_cp_wallet_asset
    ON copy_positions(wallet_id, asset);
CREATE INDEX IF NOT EXISTS idx_cp_wallet_open
    ON copy_positions(wallet_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_ct_status
    ON copy_trades(status);
CREATE INDEX IF NOT EXISTS idx_ct_asset_status
    ON copy_trades(asset, status);
CREATE INDEX IF NOT EXISTS idx_ct_slug_status
    ON copy_trades(slug, status);
CREATE INDEX IF NOT EXISTS idx_cs_reason
    ON copy_skips(reason);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ct_unique_open_asset
    ON copy_trades(asset) WHERE status IN ('pending', 'open');
CREATE INDEX IF NOT EXISTS idx_coa_asset
    ON copy_order_attempts(asset);
CREATE INDEX IF NOT EXISTS idx_coa_attempt_time
    ON copy_order_attempts(attempt_time);
"""


def _get_db() -> sqlite3.Connection:
    """Open DB, run schema bootstrap and migrations."""
    conn = sqlite3.connect(str(COPY_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # Check schema version
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]

    if "copy_schema_version" not in tables:
        # Fresh DB or pre-versioned DB — run full bootstrap
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_INDEX_SQL)
        conn.execute("INSERT INTO copy_schema_version (version) VALUES (?)",
                     (SCHEMA_VERSION,))
        # Migration: add 'copied' column to old DBs if copy_positions exists
        # but doesn't have it (backward compat, not used as truth)
        _safe_add_column(conn, "copy_positions", "copied", "INTEGER DEFAULT 0")
        conn.commit()
    else:
        current = conn.execute(
            "SELECT version FROM copy_schema_version").fetchone()
        current_ver = current[0] if current else 0
        if current_ver < SCHEMA_VERSION:
            _migrate(conn, current_ver, SCHEMA_VERSION)

    return conn


def get_schema_version(conn) -> int:
    """Read schema version from DB. Returns 0 if table missing or empty."""
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if "copy_schema_version" not in tables:
            return 0
        row = conn.execute("SELECT version FROM copy_schema_version").fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def _safe_add_column(conn, table: str, column: str, typedef: str):
    """Add a column if it doesn't exist. Explicit, no broad except."""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {typedef}")


def _migrate(conn, from_ver: int, to_ver: int):
    """Run explicit migrations between schema versions."""
    if from_ver < 2:
        # v2: add indexes, unique constraint, ensure all tables exist
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_INDEX_SQL)
        _safe_add_column(conn, "copy_positions", "copied", "INTEGER DEFAULT 0")

    if from_ver < 3:
        # v3: runtime state table + order audit table
        conn.executescript(_SCHEMA_SQL)
        conn.executescript(_INDEX_SQL)

    conn.execute("UPDATE copy_schema_version SET version=?", (to_ver,))
    conn.commit()
    log.info(f"Migrated schema from v{from_ver} to v{to_ver}")


def _db_fetchall(conn, sql, args=()):
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _db_fetchone(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row else None


# ── Runtime state helpers ─────────────────────────────────────────────────

def _get_runtime_state(conn, key: str, default: str = "") -> str:
    """Read a runtime state value from DB."""
    row = _db_fetchone(conn, "SELECT value FROM copy_runtime_state WHERE key=?", (key,))
    return row["value"] if row else default


def _set_runtime_state(conn, key: str, value: str):
    """Upsert a runtime state value."""
    now = utc_now_str()
    conn.execute(
        "INSERT INTO copy_runtime_state (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, str(value), now))
    conn.commit()


def _log_order_attempt(conn, trade_id, asset, slug, source_wallet, source_pseudonym,
                       price, stake, size, result_status, error_message="", order_id=""):
    """Record every order attempt for audit trail."""
    conn.execute("""INSERT INTO copy_order_attempts
        (trade_id, asset, slug, source_wallet, source_pseudonym,
         attempt_time, requested_price, requested_stake, requested_size,
         result_status, error_message, order_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (trade_id, asset, slug, source_wallet, source_pseudonym,
         utc_now_str(), price, stake, size, result_status, error_message, order_id))


# ── Wallet management ─────────────────────────────────────────────────────

def add_wallet(conn, wallet: str):
    """Add a wallet to watch. Fetches pseudonym from data API."""
    wallet = wallet.lower().strip()
    existing = _db_fetchone(conn, "SELECT id, enabled FROM copy_wallets WHERE wallet=?", (wallet,))
    if existing:
        if not existing["enabled"]:
            conn.execute("UPDATE copy_wallets SET enabled=1 WHERE id=?", (existing["id"],))
            conn.commit()
            print(f"  Re-enabled wallet {wallet[:10]}...")
        else:
            print(f"  Wallet already tracked: {wallet[:10]}...")
        return

    pseudonym = ""
    try:
        r = requests.get(f"{DATA_API}/activity", params={"user": wallet, "limit": 1}, timeout=10)
        if r.ok:
            data = r.json()
            if isinstance(data, list) and data:
                pseudonym = data[0].get("pseudonym", "")
    except Exception as e:
        log.warning(f"Could not fetch pseudonym for {wallet[:10]}...: {e}")

    conn.execute("INSERT INTO copy_wallets (wallet, pseudonym) VALUES (?, ?)", (wallet, pseudonym))
    conn.commit()
    print(f"  Added: {wallet[:10]}... ({pseudonym or 'unknown'})")


def remove_wallet(conn, wallet: str):
    """Disable a wallet (keeps position history)."""
    wallet = wallet.lower().strip()
    cursor = conn.execute("UPDATE copy_wallets SET enabled=0 WHERE wallet=?", (wallet,))
    conn.commit()
    if cursor.rowcount:
        print(f"  Disabled: {wallet[:10]}...")
    else:
        print(f"  Wallet not found: {wallet[:10]}...")


def list_wallets(conn):
    """Show all tracked wallets."""
    rows = _db_fetchall(conn, "SELECT * FROM copy_wallets ORDER BY added_at DESC")
    if not rows:
        print("  No wallets tracked.")
        return
    print(f"\n  {'Wallet':<44s} {'Name':<25s} {'Enabled':>8s} {'Added':>12s}")
    print(f"  {'-'*44} {'-'*25} {'-'*8} {'-'*12}")
    for r in rows:
        status = "yes" if r["enabled"] else "no"
        added = str(r["added_at"])[:10] if r["added_at"] else ""
        print(f"  {r['wallet']:<44s} {r['pseudonym'][:25]:<25s} {status:>8s} {added:>12s}")
    pos_count = _db_fetchone(conn, "SELECT COUNT(*) as c FROM copy_positions")
    print(f"\n  Total positions stored: {pos_count['c']}")


# ── Polymarket: fetch & store positions ───────────────────────────────────

def _fetch_positions(wallet: str) -> list:
    """Fetch all positions from Polymarket Data API with retry."""
    def _do_fetch():
        r = requests.get(f"{DATA_API}/positions", params={
            "user": wallet, "sizeThreshold": 0,
        }, timeout=15)
        if not r.ok:
            log.warning(f"[DataAPI] Error: {r.status_code}")
            return []
        data = r.json()
        if not isinstance(data, list):
            log.warning(f"[DataAPI] Unexpected response type: {type(data).__name__}")
            return []
        return data

    result = _retry_request(_do_fetch, max_retries=2, backoff=1.0, label="fetch_positions")
    return result if result is not None else []


def sync_wallet(conn, wallet_id: int, wallet: str, positions=None) -> dict:
    """
    Pull positions for a wallet and upsert into copy_positions.
    If positions are already fetched, pass them to avoid a redundant API call.
    Commits once at the end (batched).
    Returns {new: int, updated: int, closed: int}.
    """
    if positions is None:
        positions = _fetch_positions(wallet)
    now = utc_now_str()
    stats = {"new": 0, "updated": 0, "closed": 0}
    live_assets = set()

    for p in positions:
        asset = p.get("asset", "")
        if not asset or not isinstance(asset, str):
            continue
        # Validate numeric fields before using
        try:
            size = float(p.get("size", 0))
        except (ValueError, TypeError):
            log.warning(f"[Sync] Invalid size for asset {asset[:20]}..., skipping")
            continue
        if size <= 0:
            continue

        live_assets.add(asset)

        # Validate critical numeric fields
        def _safe_float(val, default=0.0):
            try:
                return float(val) if val is not None else default
            except (ValueError, TypeError):
                return default

        existing = _db_fetchone(conn,
            "SELECT id FROM copy_positions WHERE wallet_id=? AND asset=?",
            (wallet_id, asset))

        if existing:
            conn.execute("""
                UPDATE copy_positions SET
                    size=?, avg_price=?, initial_value=?, current_value=?,
                    cash_pnl=?, percent_pnl=?, total_bought=?, cur_price=?,
                    redeemable=?, title=?, slug=?, icon=?, event_slug=?,
                    outcome=?, outcome_index=?, opposite_outcome=?,
                    opposite_asset=?, end_date=?, negative_risk=?,
                    last_seen_at=?, closed_at=NULL
                WHERE id=?
            """, (
                size, _safe_float(p.get("avgPrice")),
                _safe_float(p.get("initialValue")), _safe_float(p.get("currentValue")),
                _safe_float(p.get("cashPnl")), _safe_float(p.get("percentPnl")),
                _safe_float(p.get("totalBought")), _safe_float(p.get("curPrice")),
                1 if p.get("redeemable") else 0,
                p.get("title", ""), p.get("slug", ""),
                p.get("icon", ""), p.get("eventSlug", ""),
                p.get("outcome", ""), int(p.get("outcomeIndex", 0)),
                p.get("oppositeOutcome", ""), p.get("oppositeAsset", ""),
                p.get("endDate", ""), 1 if p.get("negativeRisk") else 0,
                now, existing["id"],
            ))
            stats["updated"] += 1
        else:
            conn.execute("""
                INSERT INTO copy_positions
                    (wallet_id, asset, condition_id, size, avg_price,
                     initial_value, current_value, cash_pnl, percent_pnl,
                     total_bought, cur_price, redeemable, title, slug, icon,
                     event_slug, outcome, outcome_index, opposite_outcome,
                     opposite_asset, end_date, negative_risk,
                     first_seen_at, last_seen_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                wallet_id, asset, p.get("conditionId", ""),
                size, _safe_float(p.get("avgPrice")),
                _safe_float(p.get("initialValue")), _safe_float(p.get("currentValue")),
                _safe_float(p.get("cashPnl")), _safe_float(p.get("percentPnl")),
                _safe_float(p.get("totalBought")), _safe_float(p.get("curPrice")),
                1 if p.get("redeemable") else 0,
                p.get("title", ""), p.get("slug", ""),
                p.get("icon", ""), p.get("eventSlug", ""),
                p.get("outcome", ""), int(p.get("outcomeIndex", 0)),
                p.get("oppositeOutcome", ""), p.get("oppositeAsset", ""),
                p.get("endDate", ""), 1 if p.get("negativeRisk") else 0,
                now, now,
            ))
            stats["new"] += 1

    # Mark positions no longer in the API as closed
    open_positions = _db_fetchall(conn,
        "SELECT id, asset FROM copy_positions WHERE wallet_id=? AND closed_at IS NULL",
        (wallet_id,))
    for row in open_positions:
        if row["asset"] not in live_assets:
            conn.execute("UPDATE copy_positions SET closed_at=? WHERE id=?",
                         (now, row["id"]))
            stats["closed"] += 1

    conn.commit()  # single commit per wallet sync
    return stats


def sync_all(conn, wallet_filter: str = None):
    """Sync positions for all enabled wallets (or a specific one)."""
    if wallet_filter:
        wallet_filter = wallet_filter.lower().strip()
        wallets = _db_fetchall(conn,
            "SELECT * FROM copy_wallets WHERE wallet=?", (wallet_filter,))
    else:
        wallets = _db_fetchall(conn,
            "SELECT * FROM copy_wallets WHERE enabled=1")

    if not wallets:
        print("  No wallets to sync.")
        return

    for w in wallets:
        stats = sync_wallet(conn, w["id"], w["wallet"])
        name = w["pseudonym"] or w["wallet"][:10] + "..."
        print(f"  {name}: {stats['new']} new, {stats['updated']} updated, {stats['closed']} closed")


# ── Stale pending cleanup ──────────────────────────────────────────────────

PENDING_STALE_SECONDS = 60
MAX_CONSECUTIVE_ORDER_FAILURES = 3
CIRCUIT_BREAKER_DECAY_SECONDS = 3600  # reset after 1 hour of no failures


def _cleanup_stale_pending(conn):
    """Delete pending reservation rows older than PENDING_STALE_SECONDS.
    These can occur if the process crashes between reservation and order."""
    cutoff = datetime.now(timezone.utc)
    stale = _db_fetchall(conn,
        "SELECT id, asset, placed_at FROM copy_trades WHERE status='pending'")
    cleaned = 0
    for row in stale:
        try:
            placed = parse_utc_timestamp(row["placed_at"])
            age = (cutoff - placed).total_seconds()
            if age > PENDING_STALE_SECONDS:
                conn.execute("DELETE FROM copy_trades WHERE id=?", (row["id"],))
                log.warning(f"Cleaned stale pending trade: asset={row['asset'][:30]}... "
                            f"age={age:.0f}s")
                cleaned += 1
        except (ValueError, TypeError):
            # Unparseable timestamp — remove it
            conn.execute("DELETE FROM copy_trades WHERE id=?", (row["id"],))
            cleaned += 1
    if cleaned:
        conn.commit()
        log.info(f"Cleaned {cleaned} stale pending trade(s)")
    return cleaned


# ── Timestamp helpers ─────────────────────────────────────────────────────

def utc_now_str():
    """Canonical UTC timestamp string for DB storage."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def parse_utc_timestamp(s):
    """Parse a stored UTC timestamp string back to aware datetime.
    Primary format: %Y-%m-%d %H:%M:%S (what utc_now_str produces).
    Fallback: fromisoformat for any older rows in different formats."""
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt


def _today_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _retry_request(fn, max_retries=2, backoff=1.0, label="request"):
    """Retry a read-only callable on RequestException. Returns fn() result or last exception's default."""
    last_err = None
    for attempt in range(1 + max_retries):
        try:
            return fn()
        except requests.RequestException as e:
            last_err = e
            if attempt < max_retries:
                log.warning(f"[Retry] {label} attempt {attempt+1} failed: {e} — retrying in {backoff}s")
                time.sleep(backoff)
            else:
                log.warning(f"[Retry] {label} failed after {1+max_retries} attempts: {e}")
    return None  # caller must handle None


def _daily_loss_check(conn, cfg):
    """Return True if daily loss limit is exceeded (block new buys).

    Policy: realised P&L only. Unrealised losses on open trades do NOT
    trigger the kill-switch — only closed trades settled today count.
    This avoids premature blocking from temporary drawdowns on positions
    that haven't resolved yet.
    """
    today = _today_utc_str()
    row = _db_fetchone(conn,
        "SELECT COALESCE(SUM(pnl_usd), 0) as total_pnl FROM copy_trades "
        "WHERE status='closed' AND closed_at >= ?", (today,))
    realised_pnl = row["total_pnl"] if row else 0
    if realised_pnl < -cfg.max_daily_loss:
        return True, realised_pnl
    return False, realised_pnl


def _daily_trade_count(conn):
    """Count trades placed today."""
    today = _today_utc_str()
    row = _db_fetchone(conn,
        "SELECT COUNT(*) as c FROM copy_trades WHERE placed_at >= ?", (today,))
    return row["c"] if row else 0


def _wallet_open_stake(conn, source_wallet):
    """Sum of open stake for a specific source wallet."""
    row = _db_fetchone(conn,
        "SELECT COALESCE(SUM(our_stake), 0) as total FROM copy_trades "
        "WHERE source_wallet=? AND status IN ('pending','open')",
        (source_wallet,))
    return row["total"] if row else 0


def _slug_open_stake(conn, slug):
    """Sum of open stake for a specific slug."""
    if not slug:
        return 0
    row = _db_fetchone(conn,
        "SELECT COALESCE(SUM(our_stake), 0) as total FROM copy_trades "
        "WHERE slug=? AND status IN ('pending','open')", (slug,))
    return row["total"] if row else 0


def _total_open_stake(conn):
    """Sum of all open stake across all trades."""
    row = _db_fetchone(conn,
        "SELECT COALESCE(SUM(our_stake), 0) as total FROM copy_trades "
        "WHERE status IN ('pending','open')")
    return row["total"] if row else 0


def _unrealised_pnl(conn):
    """Sum of unrealised P&L for all open trades."""
    row = _db_fetchone(conn,
        "SELECT COALESCE(SUM(pnl_usd), 0) as total FROM copy_trades "
        "WHERE status='open'")
    return row["total"] if row else 0


# ── Order sizing ──────────────────────────────────────────────────────────

def determine_order_size(buy_price, min_shares, cfg):
    """
    Determine order size in shares and stake.

    Current policy: buy exactly orderMinSize shares, capped by max_shares.
    Isolated here for future upgrades (proportional, bankroll-fraction, etc).

    Returns (shares, stake_usd) or (0, 0) if cannot size.
    """
    if min_shares > cfg.max_shares:
        return 0, 0
    if buy_price <= 0 or buy_price >= 1:
        return 0, 0
    shares = min_shares
    stake = round(shares * buy_price, 2)
    return shares, stake


# ── Market validation ──────────────────────────────────────────────────────

def _check_market(slug: str, asset: str):
    """
    Validate a specific market+token on Polymarket with retry.

    Checks the Gamma API for the event, then finds the specific market
    whose token matches our asset. This prevents trading on a closed
    market within an event that has other open markets.

    Returns {accepting, order_min_size, closed} or None on error (fail-closed).
    """
    if not slug:
        return {"accepting": False, "order_min_size": 5, "closed": True}

    def _do_check():
        r = requests.get(f"{POLY_GAMMA}/events", params={"slug": slug}, timeout=10)
        data = r.json()
        if not isinstance(data, list) or not data:
            return {"accepting": False, "order_min_size": 5, "closed": True}
        ev = data[0]

        # Validate event payload shape
        if not isinstance(ev, dict):
            log.warning(f"[Gamma] Unexpected event type: {type(ev).__name__}")
            return {"accepting": False, "order_min_size": 5, "closed": True}

        if ev.get("closed"):
            return {"accepting": False, "order_min_size": 5, "closed": True}

        markets = ev.get("markets")
        if not isinstance(markets, list):
            log.warning(f"[Gamma] Missing or invalid 'markets' in event {slug}")
            return {"accepting": False, "order_min_size": 5, "closed": True}

        # Find the specific market that contains our asset token
        for mkt in markets:
            if not isinstance(mkt, dict):
                continue
            tokens_raw = mkt.get("clobTokenIds", "")
            # Parse to list — clobTokenIds can be JSON string or already a list
            if isinstance(tokens_raw, str):
                try:
                    token_list = json.loads(tokens_raw)
                except (json.JSONDecodeError, TypeError):
                    token_list = []
            elif isinstance(tokens_raw, list):
                token_list = tokens_raw
            else:
                token_list = []

            # Exact match only — no substring matching
            if asset not in token_list:
                continue

            # Found the market for this specific asset
            accepting = bool(mkt.get("acceptingOrders")) and not mkt.get("closed")
            return {
                "accepting": accepting,
                "order_min_size": int(mkt.get("orderMinSize", 5)),
                "closed": bool(mkt.get("closed")),
            }

        # Asset not found in any market of this event
        log.warning(f"Asset {asset[:20]}... not found in event {slug}")
        return {"accepting": False, "order_min_size": 5, "closed": True}

    result = _retry_request(_do_check, max_retries=2, backoff=1.0, label=f"check_market({slug})")
    return result  # None on total failure = fail closed


def _log_skip(conn, pos: dict, wallet_name: str, reason: str, details: str = ""):
    """Log a skipped trade for analysis."""
    now = utc_now_str()
    conn.execute("""INSERT INTO copy_skips
        (skipped_at, asset, slug, title, outcome, source_pseudonym, reason, details)
        VALUES (?,?,?,?,?,?,?,?)""",
        (now, pos.get("asset", ""), pos.get("slug", ""), pos.get("title", ""),
         pos.get("outcome", ""), wallet_name, reason, details))
    # commit handled by caller (batched per cycle)


def _update_trade_pnl(conn):
    """
    Update cur_price and P&L for all our open copy_trades.

    Does NOT auto-close trades based on midpoint. Trades are only closed
    when the market confirms resolution via Gamma API (acceptingOrders=False
    AND closed=True), checked separately.
    """
    open_trades = _db_fetchall(conn,
        "SELECT id, asset, slug, our_entry_price, our_stake FROM copy_trades WHERE status='open'")
    if not open_trades:
        return

    for t in open_trades:
        mid = clob_get_midpoint(t["asset"])
        if mid is None:
            continue
        entry = t["our_entry_price"]
        stake = t["our_stake"]
        if entry and entry > 0:
            pnl_pct = ((mid - entry) / entry) * 100
            pnl_usd = (mid - entry) * (stake / entry)
        else:
            pnl_pct = 0.0
            pnl_usd = 0.0

        conn.execute("""UPDATE copy_trades SET cur_price=?, pnl_pct=?, pnl_usd=?
            WHERE id=?""", (mid, pnl_pct, pnl_usd, t["id"]))

    conn.commit()


def _check_resolved_trades(conn):
    """
    Check if any open trades have resolved by querying the Gamma API.
    Only marks a trade closed when the market is confirmed closed+resolved.
    """
    open_trades = _db_fetchall(conn,
        "SELECT id, asset, slug, our_entry_price, our_stake, cur_price "
        "FROM copy_trades WHERE status='open'")
    if not open_trades:
        return

    now = utc_now_str()
    checked_slugs = {}  # cache slug -> market info

    for t in open_trades:
        slug = t.get("slug", "")
        if not slug:
            continue

        # Cache Gamma lookups per slug
        if slug not in checked_slugs:
            checked_slugs[slug] = _check_market(slug, t["asset"])

        info = checked_slugs[slug]
        if info is None:
            continue  # API error — skip, don't guess

        if info["closed"] and not info["accepting"]:
            # Market is confirmed closed — resolve the trade
            mid = t.get("cur_price") or clob_get_midpoint(t["asset"])
            if mid is None:
                mid = 0.0
            resolved = 1.0 if mid >= 0.5 else 0.0
            entry = t["our_entry_price"]
            stake = t["our_stake"]
            if entry and entry > 0:
                pnl_pct = ((resolved - entry) / entry) * 100
                pnl_usd = (resolved - entry) * (stake / entry)
            else:
                pnl_pct = 0.0
                pnl_usd = 0.0

            conn.execute("""UPDATE copy_trades SET cur_price=?, pnl_pct=?, pnl_usd=?,
                status='closed', closed_at=?, resolved_price=? WHERE id=?""",
                (mid, pnl_pct, pnl_usd, now, resolved, t["id"]))
            log.info(f"Trade resolved: {t.get('slug')} resolved={resolved:.0f} pnl=${pnl_usd:.2f}")

    conn.commit()


# ── Copy logic ──────────────────────────────────────────────────────────────

def copy_position(conn, pos: dict, wallet_name: str,
                  cfg: Config, dry_run: bool = False) -> bool:
    """
    Copy a new position from a target wallet.

    Idempotency: inserts a 'pending' reservation row in copy_trades BEFORE
    placing the order. The UNIQUE index on (asset) WHERE status IN ('pending','open')
    prevents duplicate execution across restarts or concurrent instances.

    Returns True if copied (or would have been in dry_run).
    """
    asset = pos["asset"]
    avg_price = pos["avg_price"]
    title = pos.get("title", "")
    slug = pos.get("slug", "")
    outcome = pos.get("outcome", "")

    if not asset:
        return False

    # ── Safety: persistent trading pause flag ──
    if _get_runtime_state(conn, "trading_paused", "0") == "1":
        log.info(f"Skip: trading paused | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "trading_paused", "operator pause active")
        conn.commit()
        return False

    # ── Safety: order-failure circuit breaker (persisted in DB) ──
    cb_failures = int(_get_runtime_state(conn, "consecutive_order_failures", "0"))
    # Decay: reset if last failure was > CIRCUIT_BREAKER_DECAY_SECONDS ago
    if cb_failures > 0:
        last_fail = _get_runtime_state(conn, "last_failure_at", "")
        if last_fail:
            try:
                elapsed = (datetime.now(timezone.utc) - parse_utc_timestamp(last_fail)).total_seconds()
                if elapsed > CIRCUIT_BREAKER_DECAY_SECONDS:
                    cb_failures = 0
                    _set_runtime_state(conn, "consecutive_order_failures", "0")
                    log.info("Circuit breaker reset (cooling-off period elapsed)")
            except (ValueError, TypeError):
                pass
    if cb_failures >= MAX_CONSECUTIVE_ORDER_FAILURES:
        log.warning(f"Circuit breaker: {cb_failures} consecutive order failures, "
                    f"trading paused | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "circuit_breaker",
                  f"consecutive_failures={cb_failures}")
        conn.commit()
        return False

    # ── Safety: daily loss kill-switch ──
    loss_hit, daily_pnl = _daily_loss_check(conn, cfg)
    if loss_hit:
        log.warning(f"DAILY LOSS LIMIT HIT: ${daily_pnl:.2f} (limit: -${cfg.max_daily_loss})")
        _log_skip(conn, pos, wallet_name, "daily_loss_limit",
                  f"daily_pnl=${daily_pnl:.2f} limit=-${cfg.max_daily_loss}")
        conn.commit()
        return False

    # ── Safety: daily trade count cap ──
    today_count = _daily_trade_count(conn)
    if today_count >= cfg.max_trades_per_day:
        log.warning(f"Daily trade cap reached: {today_count}/{cfg.max_trades_per_day}")
        _log_skip(conn, pos, wallet_name, "daily_trade_cap",
                  f"count={today_count} limit={cfg.max_trades_per_day}")
        conn.commit()
        return False

    # ── Safety: max open positions cap ──
    open_count = _db_fetchone(conn,
        "SELECT COUNT(*) as c FROM copy_trades WHERE status IN ('pending','open')")
    if open_count and open_count["c"] >= cfg.max_positions:
        log.info(f"Skip: position cap ({cfg.max_positions}) | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "position_cap",
                  f"cap={cfg.max_positions}")
        conn.commit()
        return False

    # ── Safety: don't buy both sides of the same game ──
    if slug:
        existing_slug = _db_fetchone(conn,
            "SELECT id, outcome FROM copy_trades WHERE slug=? AND status IN ('pending','open')",
            (slug,))
        if existing_slug:
            log.info(f"Skip: already on {slug} ({existing_slug['outcome']})")
            _log_skip(conn, pos, wallet_name, "duplicate_slug",
                      f"existing_outcome={existing_slug['outcome']}")
            conn.commit()
            return False

    # ── Safety: don't bet on finished events ──
    end_date_str = pos.get("end_date", "")
    if end_date_str:
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if end_dt < datetime.now(timezone.utc):
                log.info(f"Skip: event ended | {title[:45]}")
                _log_skip(conn, pos, wallet_name, "event_ended",
                          f"end_date={end_date_str}")
                conn.commit()
                return False
        except (ValueError, TypeError):
            pass

    # ── Price validation ──
    current_mid = clob_get_midpoint(asset)

    # Price sanity: reject missing, zero, or >= 1
    if current_mid is not None and (current_mid <= 0 or current_mid >= 1):
        log.info(f"Skip: price out of range ({current_mid}) | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "price_out_of_range",
                  f"cur_price={current_mid}")
        conn.commit()
        return False

    # If price is at extremes, market is likely resolved
    if current_mid is not None and (current_mid <= 0.005 or current_mid >= 0.995):
        log.info(f"Skip: market resolved (price={current_mid:.3f}) | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "market_resolved",
                  f"cur_price={current_mid:.4f}")
        conn.commit()
        return False

    # Price slippage: relative check for prices > 0.10, absolute for low prices
    if current_mid is not None:
        abs_slip = abs(current_mid - avg_price)
        if avg_price >= 0.10:
            rel_slip = abs_slip / avg_price
            if rel_slip > cfg.max_price_slip or abs_slip > 0.10:
                log.info(f"Skip: slipped {current_mid:.4f} vs {avg_price:.4f} ({rel_slip:.1%}) | {title[:45]}")
                _log_skip(conn, pos, wallet_name, "price_slip",
                          f"mid={current_mid:.4f} entry={avg_price:.4f} rel={rel_slip:.4f}")
                conn.commit()
                return False
        elif abs_slip > cfg.max_price_slip:
            log.info(f"Skip: slipped {current_mid:.4f} vs {avg_price:.4f} | {title[:45]}")
            _log_skip(conn, pos, wallet_name, "price_slip",
                      f"mid={current_mid:.4f} entry={avg_price:.4f} abs={abs_slip:.4f}")
            conn.commit()
            return False

    buy_price = current_mid if current_mid is not None else avg_price

    # ── Safety: confirm THIS SPECIFIC MARKET is still accepting orders ──
    market_info = _check_market(slug, asset)
    if market_info is None or not market_info["accepting"]:
        reason = "market_api_error" if market_info is None else "market_closed"
        log.info(f"Skip: {reason} | {title[:45]}")
        _log_skip(conn, pos, wallet_name, reason, f"slug={slug} asset={asset[:30]}")
        conn.commit()
        return False

    # ── Sizing ──
    min_shares = market_info["order_min_size"]
    our_shares, our_stake = determine_order_size(buy_price, min_shares, cfg)
    if our_shares == 0:
        reason = "min_size_exceeds_cap" if min_shares > cfg.max_shares else "price_invalid"
        log.info(f"Skip: {reason} (minShares={min_shares} cap={cfg.max_shares}) | {title[:45]}")
        _log_skip(conn, pos, wallet_name, reason,
                  f"orderMinSize={min_shares} max_shares={cfg.max_shares} price={buy_price}")
        conn.commit()
        return False

    # ── Safety: per-wallet exposure cap (post-trade) ──
    source_wallet = pos.get("wallet", "")
    if source_wallet:
        wallet_stake = _wallet_open_stake(conn, source_wallet)
        if wallet_stake + our_stake > cfg.max_stake_per_wallet:
            log.info(f"Skip: wallet exposure cap ${wallet_stake:.2f}+${our_stake:.2f}>${cfg.max_stake_per_wallet} | {title[:45]}")
            _log_skip(conn, pos, wallet_name, "wallet_exposure_cap",
                      f"current=${wallet_stake:.2f} proposed=${our_stake:.2f} cap=${cfg.max_stake_per_wallet}")
            conn.commit()
            return False

    # ── Safety: per-slug exposure cap (post-trade) ──
    if slug:
        slug_stake = _slug_open_stake(conn, slug)
        if slug_stake + our_stake > cfg.max_stake_per_slug:
            log.info(f"Skip: slug exposure cap ${slug_stake:.2f}+${our_stake:.2f}>${cfg.max_stake_per_slug} | {title[:45]}")
            _log_skip(conn, pos, wallet_name, "slug_exposure_cap",
                      f"current=${slug_stake:.2f} proposed=${our_stake:.2f} cap=${cfg.max_stake_per_slug}")
            conn.commit()
            return False

    # ── Safety: global exposure cap (post-trade) ──
    total_stake = _total_open_stake(conn)
    if total_stake + our_stake > cfg.max_total_exposure:
        log.info(f"Skip: global exposure cap ${total_stake:.2f}+${our_stake:.2f}>${cfg.max_total_exposure} | {title[:45]}")
        _log_skip(conn, pos, wallet_name, "global_exposure_cap",
                  f"current=${total_stake:.2f} proposed=${our_stake:.2f} cap=${cfg.max_total_exposure}")
        conn.commit()
        return False

    log.info(f"{'[DRY]' if dry_run else '[COPY]'} BUY ${our_stake:.2f} @ {buy_price:.4f} "
             f"({our_shares:.1f} shares) | {title[:45]} ({outcome}) [{wallet_name}]")

    if dry_run:
        return True

    # ── Idempotent reservation: insert 'pending' row BEFORE placing order ──
    # The UNIQUE index on (asset) WHERE status IN ('pending','open') prevents
    # duplicate trades if we crash between insert and order placement, or if
    # two instances race.
    now = utc_now_str()
    try:
        conn.execute("""INSERT INTO copy_trades
            (asset, slug, title, outcome, source_wallet, source_pseudonym,
             our_entry_price, our_stake, our_size, cur_price,
             status, placed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (asset, slug, title, outcome, pos.get("wallet", ""),
             wallet_name, buy_price, our_stake, our_shares, buy_price,
             "pending", now))
        conn.commit()
    except sqlite3.IntegrityError:
        # UNIQUE constraint — we already have a pending/open trade for this asset
        log.info(f"Skip: already have pending/open trade for this asset")
        _log_skip(conn, pos, wallet_name, "duplicate_asset",
                  f"asset={asset[:30]}")
        conn.commit()
        return False

    # Get the reservation row ID
    trade_row = _db_fetchone(conn,
        "SELECT id FROM copy_trades WHERE asset=? AND status='pending'", (asset,))
    trade_id = trade_row["id"] if trade_row else None

    # ── Place the order ──
    result = _order_manager.buy(
        asset, our_stake, buy_price,
        tp_pct=cfg.tp_pct,
        check_bal=True,
    )

    if result.get("ok"):
        order_id = result.get("order_id", "")
        order_status = result.get("status", "")

        # ── Post-order verification: require non-empty order_id ──
        if not order_id or order_id == "unknown":
            log.warning(f"Order reported success but order_id is missing/unknown — treating as unverified")
            _log_order_attempt(conn, trade_id, asset, slug, source_wallet, wallet_name,
                               buy_price, our_stake, our_shares, "unverified",
                               "order_id missing or unknown", order_id or "")
        else:
            log.info(f"Copied: order={order_id[:20]}... status={order_status}")
            _log_order_attempt(conn, trade_id, asset, slug, source_wallet, wallet_name,
                               buy_price, our_stake, our_shares, "success", "", order_id)

        # Reset circuit breaker on success
        _set_runtime_state(conn, "consecutive_order_failures", "0")

        # Promote reservation to 'open'
        conn.execute("""UPDATE copy_trades SET
            order_id=?, status='open' WHERE id=?""",
            (order_id, trade_id))
        conn.commit()
        return True
    else:
        err = result.get("error", "unknown")
        # Increment circuit breaker (persisted)
        cb_failures = int(_get_runtime_state(conn, "consecutive_order_failures", "0")) + 1
        _set_runtime_state(conn, "consecutive_order_failures", str(cb_failures))
        _set_runtime_state(conn, "last_failure_at", utc_now_str())
        log.warning(f"Order failed ({cb_failures}/{MAX_CONSECUTIVE_ORDER_FAILURES}): {err}")

        _log_order_attempt(conn, trade_id, asset, slug, source_wallet, wallet_name,
                           buy_price, our_stake, our_shares, "failed", str(err), "")

        # Delete the reservation — order never went through
        conn.execute("DELETE FROM copy_trades WHERE id=?", (trade_id,))
        _log_skip(conn, pos, wallet_name, "order_failed", str(err))
        conn.commit()
        return False


# ── Monitor loop ────────────────────────────────────────────────────────────

def run_monitor(dry_run: bool = False):
    """
    Continuously poll all enabled wallets. When a new position appears
    (asset not previously in copy_positions for that wallet), copy it.

    Entry-copy only: we copy on first detection, we do NOT mirror exits
    or adjust position sizes.
    """
    cfg = Config()
    conn = _get_db()

    # Load wallets (will refresh each cycle)
    wallets = _db_fetchall(conn, "SELECT * FROM copy_wallets WHERE enabled=1")
    if not wallets:
        log.error("No enabled wallets. Use 'add' first.")
        return

    log.info(f"Copy Scanner (entry-copy mode)")
    log.info(f"Watching {len(wallets)} wallet(s)")
    for w in wallets:
        log.info(f"  {w['pseudonym'] or w['wallet'][:10]+'...'}")
    log.info(f"Max shares: {cfg.max_shares} | Max positions: {cfg.max_positions}")
    log.info(f"Poll interval: {cfg.poll_interval}s")
    if dry_run:
        log.info(f"MODE: DRY RUN (no real orders)")

    # Startup: clean stale pending reservations from prior crashes
    cleaned = _cleanup_stale_pending(conn)
    if cleaned:
        log.warning(f"Startup: cleaned {cleaned} stale pending trade(s)")

    # Initial sync — snapshot current positions so we don't copy old ones
    log.info("Initial sync...")
    for w in wallets:
        stats = sync_wallet(conn, w["id"], w["wallet"])
        name = w["pseudonym"] or w["wallet"][:10] + "..."
        log.info(f"{name}: {stats['new']} positions stored")

    log.info("Monitoring for new positions... (Ctrl+C to stop)")

    try:
        while True:
            # Refresh wallet list each cycle (additions/removals take effect)
            wallets = _db_fetchall(conn, "SELECT * FROM copy_wallets WHERE enabled=1")

            # Check pause state once per cycle
            is_paused = _get_runtime_state(conn, "trading_paused", "0") == "1"
            if is_paused:
                log.info("Trading paused — syncing only, no new trades")

            for w in wallets:
                positions = _fetch_positions(w["wallet"])

                if is_paused:
                    # Still sync positions even when paused
                    sync_wallet(conn, w["id"], w["wallet"], positions=positions)
                    continue

                for p in positions:
                    asset = p.get("asset", "")
                    try:
                        size = float(p.get("size", 0))
                    except (ValueError, TypeError):
                        continue
                    if not asset or size <= 0:
                        continue

                    # Check if we've seen this position before for THIS wallet
                    existing = _db_fetchone(conn,
                        "SELECT id FROM copy_positions WHERE wallet_id=? AND asset=?",
                        (w["id"], asset))
                    if existing:
                        continue

                    # Quick pre-filter before heavier checks
                    try:
                        cur_price = float(p.get("curPrice", 0))
                    except (ValueError, TypeError):
                        continue
                    if cur_price <= 0.005 or cur_price >= 0.995:
                        continue

                    end_date_str = p.get("endDate", "")
                    if end_date_str:
                        try:
                            end_dt = datetime.fromisoformat(
                                end_date_str.replace("Z", "+00:00"))
                            if end_dt < datetime.now(timezone.utc):
                                continue
                        except (ValueError, TypeError):
                            pass

                    # New position — store it, then try to copy
                    now = utc_now_str()
                    name = w["pseudonym"] or w["wallet"][:10] + "..."
                    log.info(f"New position from {name}")

                    sync_wallet(conn, w["id"], w["wallet"], positions=positions)

                    copy_position(conn, {
                        "asset": asset,
                        "avg_price": float(p.get("avgPrice", 0)),
                        "initial_value": float(p.get("initialValue", 0)),
                        "title": p.get("title", ""),
                        "slug": p.get("slug", ""),
                        "outcome": p.get("outcome", ""),
                        "end_date": end_date_str,
                        "wallet": w["wallet"],
                    }, name, cfg=cfg, dry_run=dry_run)

            # Update P&L on our open trades
            _update_trade_pnl(conn)

            # Log unrealised P&L warning if materially negative
            unrealised = _unrealised_pnl(conn)
            if unrealised < -cfg.max_daily_loss:
                log.warning(f"Unrealised P&L ${unrealised:+.2f} exceeds daily loss cap "
                            f"(${cfg.max_daily_loss}) — informational only, not blocking")

            # Check for resolved trades (confirmed via Gamma, not midpoint)
            _check_resolved_trades(conn)

            # Clean stale pending reservations each cycle
            _cleanup_stale_pending(conn)

            # Reload settings each cycle (max_shares etc may change)
            try:
                cfg = Config()
            except Exception as e:
                log.warning(f"Config reload failed (keeping previous): {e}")

            open_trades = _db_fetchone(conn,
                "SELECT COUNT(*) as c FROM copy_trades WHERE status='open'")
            trade_count = open_trades["c"] if open_trades else 0
            total_exp = _total_open_stake(conn)
            log.debug(f"Cycle complete | {trade_count} open trades | exposure ${total_exp:.2f}")
            time.sleep(cfg.poll_interval)
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        conn.close()


# ── Status ──────────────────────────────────────────────────────────────────

def print_status():
    """Show wallet positions and our copy trades separately."""
    cfg = Config()
    conn = _get_db()

    # Pause state
    paused = _get_runtime_state(conn, "trading_paused", "0") == "1"
    if paused:
        print(f"\n  ** TRADING PAUSED **")

    # ── Section 1: Tracked wallet positions ──
    wallets = _db_fetchall(conn, "SELECT * FROM copy_wallets WHERE enabled=1")
    print(f"\n  === Tracked Wallets ({len(wallets)}) ===")

    for w in wallets:
        name = w["pseudonym"] or w["wallet"][:10] + "..."
        open_pos = _db_fetchall(conn,
            "SELECT * FROM copy_positions WHERE wallet_id=? AND closed_at IS NULL "
            "ORDER BY first_seen_at DESC",
            (w["id"],))
        closed_count = _db_fetchone(conn,
            "SELECT COUNT(*) as c FROM copy_positions WHERE wallet_id=? AND closed_at IS NOT NULL",
            (w["id"],))

        print(f"\n  {name} — {len(open_pos)} open, {closed_count['c']} closed positions")
        if open_pos:
            print(f"  {'Title':<45s} {'Outcome':<12s} {'Avg':>7s} {'Cur':>7s} {'PnL%':>8s}")
            print(f"  {'-'*45} {'-'*12} {'-'*7} {'-'*7} {'-'*8}")
            for p in open_pos[:15]:
                pnl = p["percent_pnl"]
                print(f"  {p['title'][:45]:<45s} {p['outcome'][:12]:<12s} "
                      f"{p['avg_price']:>7.3f} {p['cur_price']:>7.3f} "
                      f"{pnl:>+7.1f}%")

    # ── Section 2: Our copy trades ──
    open_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='open' ORDER BY placed_at DESC")
    pending_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='pending' ORDER BY placed_at DESC")
    closed_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 10")

    print(f"\n  === Our Copy Trades ===")
    print(f"  Open: {len(open_trades)} | Pending: {len(pending_trades)}")

    if open_trades:
        total_pnl = sum(t.get("pnl_usd", 0) or 0 for t in open_trades)
        total_stake = sum(t.get("our_stake", 0) or 0 for t in open_trades)
        print(f"  Total staked: ${total_stake:.2f} | Unrealised P&L: ${total_pnl:+.2f}")
        print(f"\n  {'Title':<40s} {'Out':<8s} {'Entry':>6s} {'Cur':>6s} {'P&L':>8s} {'Source':<15s}")
        print(f"  {'-'*40} {'-'*8} {'-'*6} {'-'*6} {'-'*8} {'-'*15}")
        for t in open_trades:
            cur = t.get("cur_price") or 0
            pnl = t.get("pnl_usd") or 0
            src = (t.get("source_pseudonym") or "")[:15]
            print(f"  {(t['title'] or '')[:40]:<40s} {(t['outcome'] or '')[:8]:<8s} "
                  f"{t['our_entry_price']:>6.3f} {cur:>6.3f} "
                  f"${pnl:>+6.2f} {src:<15s}")

    if closed_trades:
        total_closed_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed_trades)
        print(f"\n  Last {len(closed_trades)} closed trades (P&L: ${total_closed_pnl:+.2f}):")
        for t in closed_trades:
            pnl = t.get("pnl_usd") or 0
            print(f"  {(t['title'] or '')[:45]:<45s} ${pnl:>+6.2f}  {t.get('closed_at','')}")

    # ── Section 3: Skip summary ──
    skip_summary = _db_fetchall(conn,
        "SELECT reason, COUNT(*) as c FROM copy_skips GROUP BY reason ORDER BY c DESC")
    if skip_summary:
        print(f"\n  === Skip Reasons ===")
        for s in skip_summary:
            print(f"  {s['reason']:<25s} {s['c']:>5d}")

    conn.close()


# ── Shared validation helpers ────────────────────────────────────────────

def _check_db_health():
    """Validate DB opens and schema version matches.
    Returns (conn, version, error_msg). conn is None on failure."""
    try:
        conn = _get_db()
        ver = get_schema_version(conn)
        if ver != SCHEMA_VERSION:
            return conn, ver, f"schema mismatch: DB=v{ver} expected=v{SCHEMA_VERSION}"
        return conn, ver, None
    except Exception as e:
        return None, 0, str(e)


def _check_settings_health():
    """Validate settings load correctly.
    Returns (cfg, error_msg). cfg is None on failure."""
    try:
        cfg = Config()
        return cfg, None
    except Exception as e:
        return None, str(e)


def _check_api_health():
    """Validate Gamma API is reachable and returning sane data (with retry).
    Returns (ok, detail_msg)."""
    def _do_check():
        r = requests.get(f"{POLY_GAMMA}/events", params={"slug": "test"}, timeout=10)
        if r.status_code >= 500:
            return False, f"server error ({r.status_code})"
        if r.status_code >= 400:
            return True, f"reachable ({r.status_code})"
        data = r.json()
        if not isinstance(data, list):
            return False, f"unexpected response type: {type(data).__name__}"
        return True, f"healthy ({r.status_code}, {len(data)} events)"

    result = _retry_request(_do_check, max_retries=2, backoff=1.0, label="api_health")
    if result is None:
        return False, "all retries failed"
    return result


def _count_stale_pending(conn):
    """Count pending trades older than PENDING_STALE_SECONDS."""
    stale = _db_fetchall(conn,
        "SELECT placed_at FROM copy_trades WHERE status='pending'")
    count = 0
    cutoff = datetime.now(timezone.utc)
    for row in stale:
        try:
            placed = parse_utc_timestamp(row["placed_at"])
            if (cutoff - placed).total_seconds() > PENDING_STALE_SECONDS:
                count += 1
        except (ValueError, TypeError):
            count += 1
    return count


# ── Health Check ──────────────────────────────────────────────────────────

def print_health():
    """Quick operational health snapshot."""
    errors = []

    # DB
    conn, db_ver, db_err = _check_db_health()
    if db_err:
        print(f"  DB: FAILED — {db_err}")
        errors.append("db")
    else:
        print(f"  DB: OK (schema v{db_ver})")

    # Settings
    cfg, cfg_err = _check_settings_health()
    if cfg_err:
        print(f"  Settings: FAILED — {cfg_err}")
        errors.append("settings")
    else:
        print(f"  Settings: OK")
        print(f"    max_shares={cfg.max_shares}  max_positions={cfg.max_positions}  "
              f"poll={cfg.poll_interval}s  slip={cfg.max_price_slip}")
        print(f"    daily_loss_cap=${cfg.max_daily_loss}  trades/day={cfg.max_trades_per_day}  "
              f"wallet_cap=${cfg.max_stake_per_wallet}  slug_cap=${cfg.max_stake_per_slug}")

    if conn:
        # Pause state
        paused = _get_runtime_state(conn, "trading_paused", "0") == "1"
        print(f"  Trading: {'PAUSED' if paused else 'active'}")

        # Circuit breaker
        cb_failures = int(_get_runtime_state(conn, "consecutive_order_failures", "0"))
        cb_status = f"TRIPPED ({cb_failures}/{MAX_CONSECUTIVE_ORDER_FAILURES})" if cb_failures >= MAX_CONSECUTIVE_ORDER_FAILURES else f"ok ({cb_failures}/{MAX_CONSECUTIVE_ORDER_FAILURES})"
        print(f"  Circuit breaker: {cb_status}")

        # Wallets
        wallets = _db_fetchall(conn, "SELECT * FROM copy_wallets WHERE enabled=1")
        disabled = _db_fetchone(conn,
            "SELECT COUNT(*) as c FROM copy_wallets WHERE enabled=0")
        print(f"  Wallets: {len(wallets)} enabled, {disabled['c'] if disabled else 0} disabled")

        # Trades
        open_t = _db_fetchone(conn,
            "SELECT COUNT(*) as c FROM copy_trades WHERE status='open'")
        pending_t = _db_fetchone(conn,
            "SELECT COUNT(*) as c FROM copy_trades WHERE status='pending'")
        closed_t = _db_fetchone(conn,
            "SELECT COUNT(*) as c FROM copy_trades WHERE status='closed'")
        print(f"  Trades: {open_t['c']} open, {pending_t['c']} pending, {closed_t['c']} closed")

        # Exposure
        total_exposure = _total_open_stake(conn)
        if cfg:
            print(f"  Exposure: ${total_exposure:.2f} / ${cfg.max_total_exposure:.2f}")

        # Unrealised P&L
        unrealised = _unrealised_pnl(conn)
        print(f"  Unrealised P&L: ${unrealised:+.2f}")

        # Stale pending
        stale_count = _count_stale_pending(conn)
        if stale_count:
            print(f"  Stale pending: {stale_count} (will be cleaned on next monitor cycle)")
        else:
            print(f"  Stale pending: 0")

        # Daily stats
        if cfg:
            loss_hit, daily_pnl = _daily_loss_check(conn, cfg)
            today_count = _daily_trade_count(conn)
            status_str = "BLOCKED" if loss_hit else "ok"
            print(f"  Today: {today_count} trades, realised P&L ${daily_pnl:+.2f} [{status_str}]")

        conn.close()

    # API
    api_ok, api_detail = _check_api_health()
    if api_ok:
        print(f"  Gamma API: {api_detail}")
    else:
        print(f"  Gamma API: FAILED — {api_detail}")
        errors.append("api")

    if errors:
        print(f"\n  HEALTH: DEGRADED — issues with: {', '.join(errors)}")
    else:
        print(f"\n  HEALTH: OK")


def print_trades():
    """Show trade-focused view: open, pending, and recent closed."""
    conn = _get_db()

    open_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='open' ORDER BY placed_at DESC")
    pending_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='pending' ORDER BY placed_at DESC")
    closed_trades = _db_fetchall(conn,
        "SELECT * FROM copy_trades WHERE status='closed' ORDER BY closed_at DESC LIMIT 20")

    print(f"\n  === Open Trades ({len(open_trades)}) ===")
    if open_trades:
        total_stake = sum(t.get("our_stake", 0) or 0 for t in open_trades)
        unrealised = sum(t.get("pnl_usd", 0) or 0 for t in open_trades)
        print(f"  Total staked: ${total_stake:.2f} | Unrealised P&L: ${unrealised:+.2f}")
        print(f"\n  {'Title':<40s} {'Out':<8s} {'Entry':>6s} {'Cur':>6s} {'P&L':>8s} {'Source':<15s}")
        print(f"  {'-'*40} {'-'*8} {'-'*6} {'-'*6} {'-'*8} {'-'*15}")
        for t in open_trades:
            cur = t.get("cur_price") or 0
            pnl = t.get("pnl_usd") or 0
            src = (t.get("source_pseudonym") or "")[:15]
            print(f"  {(t['title'] or '')[:40]:<40s} {(t['outcome'] or '')[:8]:<8s} "
                  f"{t['our_entry_price']:>6.3f} {cur:>6.3f} "
                  f"${pnl:>+6.2f} {src:<15s}")

    if pending_trades:
        print(f"\n  === Pending Trades ({len(pending_trades)}) ===")
        for t in pending_trades:
            print(f"  {(t['title'] or '')[:50]:<50s} placed={t.get('placed_at','')}")

    if closed_trades:
        total_closed_pnl = sum(t.get("pnl_usd", 0) or 0 for t in closed_trades)
        print(f"\n  === Recent Closed ({len(closed_trades)}, P&L: ${total_closed_pnl:+.2f}) ===")
        for t in closed_trades:
            pnl = t.get("pnl_usd") or 0
            print(f"  {(t['title'] or '')[:45]:<45s} ${pnl:>+6.2f}  {t.get('closed_at','')}")

    conn.close()


def run_cleanup():
    """Clean stale pending trades and optionally reset circuit breaker."""
    conn = _get_db()

    cleaned = _cleanup_stale_pending(conn)
    if cleaned:
        print(f"  Cleaned {cleaned} stale pending trade(s)")
    else:
        print(f"  No stale pending trades")

    cb_failures = int(_get_runtime_state(conn, "consecutive_order_failures", "0"))
    if cb_failures > 0:
        _set_runtime_state(conn, "consecutive_order_failures", "0")
        print(f"  Reset circuit breaker (was at {cb_failures}/{MAX_CONSECUTIVE_ORDER_FAILURES})")
    else:
        print(f"  Circuit breaker: ok (0/{MAX_CONSECUTIVE_ORDER_FAILURES})")

    conn.close()


def print_config():
    """Print current config and runtime state."""
    cfg, cfg_err = _check_settings_health()
    if cfg_err:
        print(f"  Settings: FAILED — {cfg_err}")
        return

    print(f"\n  === Config (from settings.json) ===")
    print(f"  max_shares:           {cfg.max_shares}")
    print(f"  max_positions:        {cfg.max_positions}")
    print(f"  tp_pct:               {cfg.tp_pct}")
    print(f"  poll_interval:        {cfg.poll_interval}s")
    print(f"  max_price_slip:       {cfg.max_price_slip}")
    print(f"  max_daily_loss:       ${cfg.max_daily_loss}")
    print(f"  max_trades_per_day:   {cfg.max_trades_per_day}")
    print(f"  max_stake_per_wallet: ${cfg.max_stake_per_wallet}")
    print(f"  max_stake_per_slug:   ${cfg.max_stake_per_slug}")
    print(f"  max_total_exposure:   ${cfg.max_total_exposure}")

    conn = _get_db()
    print(f"\n  === Runtime State ===")
    paused = _get_runtime_state(conn, "trading_paused", "0")
    print(f"  trading_paused:       {'YES' if paused == '1' else 'no'}")
    cb = _get_runtime_state(conn, "consecutive_order_failures", "0")
    print(f"  circuit_breaker:      {cb}/{MAX_CONSECUTIVE_ORDER_FAILURES}")
    last_fail = _get_runtime_state(conn, "last_failure_at", "never")
    print(f"  last_failure_at:      {last_fail}")

    print(f"\n  === Daily Stats ===")
    loss_hit, daily_pnl = _daily_loss_check(conn, cfg)
    today_count = _daily_trade_count(conn)
    total_exp = _total_open_stake(conn)
    unrealised = _unrealised_pnl(conn)
    print(f"  trades_today:         {today_count}/{cfg.max_trades_per_day}")
    print(f"  realised_pnl_today:   ${daily_pnl:+.2f} (limit: -${cfg.max_daily_loss})")
    print(f"  unrealised_pnl:       ${unrealised:+.2f}")
    print(f"  total_exposure:       ${total_exp:.2f} / ${cfg.max_total_exposure}")
    print(f"  daily_loss_block:     {'YES' if loss_hit else 'no'}")

    conn.close()


def startup_checks():
    """Run critical checks before starting the monitor. Returns True if safe to proceed."""
    ok = True

    conn, db_ver, db_err = _check_db_health()
    if db_err:
        log.error(f"Database check failed: {db_err}")
        ok = False
    if conn:
        conn.close()

    cfg, cfg_err = _check_settings_health()
    if cfg_err:
        log.error(f"Settings check failed: {cfg_err}")
        ok = False

    api_ok, api_detail = _check_api_health()
    if not api_ok:
        log.warning(f"Gamma API: {api_detail} (will retry in loop)")

    return ok


# ── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Copy Scanner — entry-copy wallet positions")
    parser.add_argument("command", nargs="?", default="monitor",
                        choices=["monitor", "add", "remove", "wallets", "sync",
                                 "status", "health", "pause", "resume",
                                 "trades", "cleanup", "config"],
                        help="Command to run")
    parser.add_argument("wallet", nargs="?", default="",
                        help="Wallet address (for add/remove/sync)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show trades without placing orders")
    args = parser.parse_args()

    if args.command in ("add", "remove") and not args.wallet:
        print("ERROR: Specify a wallet address")
        sys.exit(1)

    conn = _get_db()

    if args.command == "add":
        add_wallet(conn, args.wallet)
    elif args.command == "remove":
        remove_wallet(conn, args.wallet)
    elif args.command == "wallets":
        list_wallets(conn)
    elif args.command == "sync":
        sync_all(conn, wallet_filter=args.wallet or None)
    elif args.command == "pause":
        _set_runtime_state(conn, "trading_paused", "1")
        print("  Trading PAUSED. Monitor will continue syncing but not place trades.")
        print("  Use 'resume' to re-enable trading.")
    elif args.command == "resume":
        _set_runtime_state(conn, "trading_paused", "0")
        _set_runtime_state(conn, "consecutive_order_failures", "0")
        print("  Trading RESUMED. Circuit breaker reset.")
    elif args.command == "trades":
        conn.close()
        print_trades()
        sys.exit(0)
    elif args.command == "cleanup":
        run_cleanup()
    elif args.command == "config":
        conn.close()
        print_config()
        sys.exit(0)
    elif args.command == "status":
        conn.close()
        print_status()
        sys.exit(0)
    elif args.command == "health":
        conn.close()
        print_health()
        sys.exit(0)
    elif args.command == "monitor":
        conn.close()
        if not startup_checks():
            log.error("Startup checks failed — refusing to start. Run 'health' for details.")
            sys.exit(1)
        run_monitor(dry_run=args.dry_run)
        sys.exit(0)

    conn.close()
