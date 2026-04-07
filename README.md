# Copy Scanner

Entry-copy wallet scanner for Polymarket. Monitors target wallets and copies new positions once (no exit mirroring, no top-ups).

## Architecture

- **copy_scanner.py** — main scanner: wallet management, position sync, entry-copy execution, P&L tracking, risk controls
- **taskman/** — local web dashboard (port 8081) for process management, database browsing, and settings editing
- **SCHEMA.sql** — full database schema for both `copy_scanner.db` and `cross_platform_arb.db`
- **settings.example.json** — all runtime configuration keys with current values

## State Model

- `copy_positions` — observed target wallet positions (read-only mirror of what wallets hold)
- `copy_trades` — our actual executed trades (source of truth for P&L and exposure)
- `copy_skips` — audit log of skipped trade opportunities with reasons

## Key Design Decisions

- **Entry-copy only**: we copy on first detection of a new position, never mirror exits or top-ups
- **Idempotent execution**: `pending` reservation row + UNIQUE partial index prevents duplicate trades across restarts
- **Stale pending cleanup**: pending rows older than 60s are auto-deleted (crash recovery)
- **Risk kill-switches**: daily loss cap, daily trade count cap, per-wallet exposure cap, per-slug exposure cap
- **Exposure caps check post-trade**: `current_exposure + proposed_stake > cap`, not just `current >= cap`
- **Daily loss = realised only**: unrealised drawdowns on open trades don't trigger the kill-switch
- **Fail closed**: missing API data or market validation failure = skip the trade, don't guess
- **Exact token matching**: `clobTokenIds` parsed via `json.loads()`, no substring matching
- **Config from settings.json**: all thresholds are configurable, nothing hardcoded. Missing keys log a warning and use safe defaults.

## Dependencies (not included)

- `polymarket_scanner.py` — provides `clob_get_midpoint()` and executor constants
- `Backend/order_manager.py` — provides `OrderManager.buy()` for order execution
- Polymarket APIs: Data API, CLOB API, Gamma API

## Dashboard (taskman/)

Vanilla JS web app on port 8081. Tabs:

- **Tasks** — start/stop local processes (Python scripts, caffeinate, SSH proxy)
- **Wallets / Positions / Trades / Skips** — paginated, sortable database table viewers
- **XArb Scans / Alerts** — cross-platform arbitrage data
- **Settings** — grouped form editor for all settings.json values
