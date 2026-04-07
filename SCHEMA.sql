-- =============================================================
-- copy_scanner.db — full schema
-- =============================================================

CREATE TABLE copy_wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet TEXT NOT NULL UNIQUE,
    pseudonym TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1,
    added_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE copy_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_id INTEGER NOT NULL,
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
    icon TEXT DEFAULT NULL,
    event_slug TEXT DEFAULT '',
    outcome TEXT DEFAULT '',
    outcome_index INTEGER DEFAULT 0,
    opposite_outcome TEXT DEFAULT '',
    opposite_asset TEXT DEFAULT '',
    end_date TEXT DEFAULT '',
    negative_risk INTEGER DEFAULT 0,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    closed_at TEXT DEFAULT NULL,
    copied INTEGER DEFAULT 0,
    UNIQUE(wallet_id, asset),
    FOREIGN KEY (wallet_id) REFERENCES copy_wallets(id)
);

CREATE TABLE copy_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT,
    asset TEXT NOT NULL,
    slug TEXT,
    title TEXT,
    outcome TEXT,
    source_wallet TEXT,
    source_pseudonym TEXT,
    our_entry_price REAL,
    our_stake REAL,
    our_size REAL,
    cur_price REAL,
    pnl_pct REAL,
    pnl_usd REAL,
    status TEXT DEFAULT 'open',
    placed_at TEXT NOT NULL,
    closed_at TEXT,
    resolved_price REAL
);

CREATE TABLE copy_skips (
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

CREATE TABLE copy_schema_version (
    version INTEGER NOT NULL
);

-- Indexes
CREATE INDEX idx_cp_wallet_asset ON copy_positions(wallet_id, asset);
CREATE INDEX idx_cp_wallet_open ON copy_positions(wallet_id, closed_at);
CREATE INDEX idx_ct_status ON copy_trades(status);
CREATE INDEX idx_ct_asset_status ON copy_trades(asset, status);
CREATE INDEX idx_ct_slug_status ON copy_trades(slug, status);
CREATE INDEX idx_cs_reason ON copy_skips(reason);
CREATE UNIQUE INDEX idx_ct_unique_open_asset ON copy_trades(asset) WHERE status IN ('pending', 'open');


