# trade-logger

FastAPI trade logging service with SQLite. Logs signals, entries, and exits for options/stocks/crypto trading.

## Setup

```bash
pip install -r requirements.txt
```

## Run

```bash
uvicorn app:app --port 8010
```

Or with auto-reload for development:

```bash
uvicorn app:app --port 8010 --reload
```

## Endpoints

### Write

| Method | Path | Description |
|--------|------|-------------|
| POST | `/signal` | Log a trading signal |
| POST | `/entry` | Log a trade entry |
| POST | `/exit` | Log a trade exit |

### Read

| Method | Path | Description |
|--------|------|-------------|
| GET | `/trades` | List entries with exits joined. Filters: `?ticker=SPY&broker=ibkr&date=2026-02-22` |
| GET | `/stats` | Aggregate stats: win rate, avg P&L, by ticker/broker breakdowns |
| GET | `/signals` | List signals. Filter: `?traded=true\|false` |
| GET | `/daily` | Last 30 days daily summary |
| GET | `/health` | Service health + row counts |

## Schema

Three tables: `signals`, `entries`, `exits`. See `database.py` for full DDL.

## LaunchAgent

Installed at `~/Library/LaunchAgents/com.trade-logger.plist`. Starts on boot, keeps alive, logs to `./logs/server.log`.

```bash
# Load
launchctl load ~/Library/LaunchAgents/com.trade-logger.plist

# Unload
launchctl unload ~/Library/LaunchAgents/com.trade-logger.plist
```
