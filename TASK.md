Build a trade logging service. FastAPI app, port 8010, SQLite database.

## What to build

### Schema (trades.db)
```sql
CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    ts DATETIME,
    source TEXT,        -- 'tv_webhook' | 'flow_scanner' | 'manual'
    ticker TEXT,
    direction TEXT,     -- 'CALL' | 'PUT' | 'LONG' | 'SHORT'
    signal_score FLOAT,
    indicators TEXT,    -- JSON blob
    proposed BOOLEAN DEFAULT 0,
    traded BOOLEAN DEFAULT 0,
    skip_reason TEXT
);

CREATE TABLE entries (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    ts DATETIME,
    broker TEXT,        -- 'ibkr' | 'tradier'
    ticker TEXT,
    instrument TEXT,    -- 'option' | 'spread' | 'stock' | 'crypto' | 'kalshi'
    direction TEXT,
    strike FLOAT,
    expiry TEXT,
    contracts INTEGER,
    entry_price FLOAT,
    total_cost FLOAT,
    account_size_at_entry FLOAT,
    pct_of_account FLOAT
);

CREATE TABLE exits (
    id TEXT PRIMARY KEY,
    entry_id TEXT,
    ts DATETIME,
    exit_price FLOAT,
    contracts INTEGER,
    pnl FLOAT,
    pnl_pct FLOAT,
    exit_reason TEXT,   -- 'tier_1' | 'tier_2' | 'tier_3' | 'runner' | 'stop_loss' | 'manual' | 'expiry'
    hold_time_min INTEGER
);
```

### API endpoints
- POST /signal — log a signal, body: {id, source, ticker, direction, signal_score, indicators, proposed, traded, skip_reason}
- POST /entry — log an entry, body: {id, signal_id, broker, ticker, instrument, direction, strike, expiry, contracts, entry_price, total_cost, account_size_at_entry}
- POST /exit — log an exit, body: {id, entry_id, exit_price, contracts, pnl, pnl_pct, exit_reason, hold_time_min}
- GET /trades — list entries with their exits joined, optional ?ticker=SPY&broker=ibkr&date=2026-02-22
- GET /stats — aggregate stats: total trades, win rate, avg P&L, avg win, avg loss, best trade, worst trade, total P&L, by_ticker breakdown, by_broker breakdown
- GET /signals — list signals with ?traded=true|false filter
- GET /daily — last 30 days daily summary: date, trades, wins, losses, pnl
- GET /health — {ok: true, db_path, signal_count, entry_count, exit_count}

### Files to create
- app.py — FastAPI app with all endpoints
- database.py — SQLite setup, WAL mode, connection management
- models.py — Pydantic models for request/response
- requirements.txt — fastapi uvicorn pydantic
- .env.example — PORT=8010 DB_PATH=./trades.db
- README.md — how to run, endpoint docs

### Rules
- All IDs are uuids (str), auto-generated if not provided
- All timestamps default to now() if not provided  
- Use WAL mode for SQLite (concurrent reads)
- All endpoints fire-and-forget safe (return 200 even if DB write slow)
- No auth needed (localhost only)
- pnl_pct auto-calculated on exit if not provided: ((exit_price - entry_price) / entry_price) * 100

### LaunchAgent
Create ~/Library/LaunchAgents/com.trade-logger.plist that:
- Runs: /opt/homebrew/bin/python3 -m uvicorn app:app --port 8010
- WorkingDirectory: ~/projects/trade-logger
- RunAtLoad: true, KeepAlive: true
- Log to ~/projects/trade-logger/logs/server.log

### Test it
After building, run a quick test:
- Start the server
- POST a signal, entry, and exit
- GET /stats and verify the win rate / pnl is correct
- Stop the server

### GitHub
Create the repo and push:
gh repo create masumisajjad/trade-logger --public --source=. --remote=origin --push

### Notify when done
openclaw system event --text "Trade Logger done: FastAPI on port 8010, SQLite schema, all endpoints built, LaunchAgent installed, pushed to GitHub" --mode now
