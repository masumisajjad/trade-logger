import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "./trades.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS signals (
            id TEXT PRIMARY KEY,
            ts DATETIME,
            source TEXT,
            ticker TEXT,
            direction TEXT,
            signal_score FLOAT,
            indicators TEXT,
            proposed BOOLEAN DEFAULT 0,
            traded BOOLEAN DEFAULT 0,
            skip_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS entries (
            id TEXT PRIMARY KEY,
            signal_id TEXT,
            ts DATETIME,
            broker TEXT,
            ticker TEXT,
            instrument TEXT,
            direction TEXT,
            strike FLOAT,
            expiry TEXT,
            contracts INTEGER,
            entry_price FLOAT,
            total_cost FLOAT,
            account_size_at_entry FLOAT,
            pct_of_account FLOAT
        );

        CREATE TABLE IF NOT EXISTS exits (
            id TEXT PRIMARY KEY,
            entry_id TEXT,
            ts DATETIME,
            exit_price FLOAT,
            contracts INTEGER,
            pnl FLOAT,
            pnl_pct FLOAT,
            exit_reason TEXT,
            hold_time_min INTEGER
        );
    """)
    conn.commit()
    conn.close()
