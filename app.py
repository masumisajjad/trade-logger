from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from fastapi import FastAPI, Query

from database import DB_PATH, get_conn, init_db
from models import (
    DailyOut,
    EntryIn,
    ExitIn,
    HealthOut,
    SignalIn,
    StatsOut,
    TradeOut,
)

app = FastAPI(title="Trade Logger", version="1.0.0")


@app.on_event("startup")
def startup():
    init_db()


# ── POST endpoints ────────────────────────────────────────


@app.post("/signal")
def log_signal(s: SignalIn):
    ts = s.ts or datetime.utcnow()
    conn = get_conn()
    conn.execute(
        """INSERT INTO signals (id, ts, source, ticker, direction, signal_score,
           indicators, proposed, traded, skip_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (s.id, ts.isoformat(), s.source, s.ticker, s.direction, s.signal_score,
         s.indicators, int(s.proposed), int(s.traded), s.skip_reason),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": s.id}


@app.post("/entry")
def log_entry(e: EntryIn):
    ts = e.ts or datetime.utcnow()
    conn = get_conn()
    conn.execute(
        """INSERT INTO entries (id, signal_id, ts, broker, ticker, instrument,
           direction, strike, expiry, contracts, entry_price, total_cost,
           account_size_at_entry, pct_of_account)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (e.id, e.signal_id, ts.isoformat(), e.broker, e.ticker, e.instrument,
         e.direction, e.strike, e.expiry, e.contracts, e.entry_price,
         e.total_cost, e.account_size_at_entry, e.pct_of_account),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": e.id}


@app.post("/exit")
def log_exit(x: ExitIn):
    ts = x.ts or datetime.utcnow()
    conn = get_conn()

    # Auto-calculate pnl_pct if not provided
    pnl_pct = x.pnl_pct
    if pnl_pct is None:
        row = conn.execute(
            "SELECT entry_price FROM entries WHERE id = ?", (x.entry_id,)
        ).fetchone()
        if row and row["entry_price"]:
            entry_price = row["entry_price"]
            pnl_pct = round(((x.exit_price - entry_price) / entry_price) * 100, 2)

    conn.execute(
        """INSERT INTO exits (id, entry_id, ts, exit_price, contracts, pnl,
           pnl_pct, exit_reason, hold_time_min)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (x.id, x.entry_id, ts.isoformat(), x.exit_price, x.contracts,
         x.pnl, pnl_pct, x.exit_reason, x.hold_time_min),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "id": x.id}


# ── GET endpoints ─────────────────────────────────────────


@app.get("/trades")
def list_trades(
    ticker: Optional[str] = Query(None),
    broker: Optional[str] = Query(None),
    date: Optional[str] = Query(None),
):
    conn = get_conn()

    where, params = [], []
    if ticker:
        where.append("e.ticker = ?")
        params.append(ticker)
    if broker:
        where.append("e.broker = ?")
        params.append(broker)
    if date:
        where.append("DATE(e.ts) = ?")
        params.append(date)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = conn.execute(
        f"SELECT * FROM entries e{clause} ORDER BY e.ts DESC", params
    ).fetchall()

    trades = []
    for r in rows:
        entry = dict(r)
        exits = conn.execute(
            "SELECT * FROM exits WHERE entry_id = ? ORDER BY ts", (entry["id"],)
        ).fetchall()
        trade = TradeOut(**entry, exits=[dict(ex) for ex in exits])
        trades.append(trade)

    conn.close()
    return trades


@app.get("/stats")
def get_stats():
    conn = get_conn()

    exits = conn.execute(
        """SELECT ex.pnl, ex.entry_id, e.ticker, e.broker
           FROM exits ex JOIN entries e ON ex.entry_id = e.id"""
    ).fetchall()

    if not exits:
        conn.close()
        return StatsOut()

    pnls = [r["pnl"] for r in exits if r["pnl"] is not None]
    if not pnls:
        conn.close()
        return StatsOut(total_trades=len(exits))

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    # by_ticker
    by_ticker: dict[str, dict] = {}
    for r in exits:
        t = r["ticker"] or "unknown"
        if t not in by_ticker:
            by_ticker[t] = {"trades": 0, "pnl": 0.0, "wins": 0}
        by_ticker[t]["trades"] += 1
        pnl_val = r["pnl"] or 0.0
        by_ticker[t]["pnl"] = round(by_ticker[t]["pnl"] + pnl_val, 2)
        if pnl_val > 0:
            by_ticker[t]["wins"] += 1
    for t in by_ticker:
        by_ticker[t]["win_rate"] = round(
            (by_ticker[t]["wins"] / by_ticker[t]["trades"]) * 100, 1
        )

    # by_broker
    by_broker: dict[str, dict] = {}
    for r in exits:
        b = r["broker"] or "unknown"
        if b not in by_broker:
            by_broker[b] = {"trades": 0, "pnl": 0.0, "wins": 0}
        by_broker[b]["trades"] += 1
        pnl_val = r["pnl"] or 0.0
        by_broker[b]["pnl"] = round(by_broker[b]["pnl"] + pnl_val, 2)
        if pnl_val > 0:
            by_broker[b]["wins"] += 1
    for b in by_broker:
        by_broker[b]["win_rate"] = round(
            (by_broker[b]["wins"] / by_broker[b]["trades"]) * 100, 1
        )

    conn.close()
    return StatsOut(
        total_trades=len(pnls),
        win_rate=round((len(wins) / len(pnls)) * 100, 1),
        avg_pnl=round(sum(pnls) / len(pnls), 2),
        avg_win=round(sum(wins) / len(wins), 2) if wins else 0.0,
        avg_loss=round(sum(losses) / len(losses), 2) if losses else 0.0,
        best_trade=max(pnls),
        worst_trade=min(pnls),
        total_pnl=round(sum(pnls), 2),
        by_ticker=by_ticker,
        by_broker=by_broker,
    )


@app.get("/signals")
def list_signals(traded: Optional[str] = Query(None)):
    conn = get_conn()
    if traded is not None:
        val = 1 if traded.lower() == "true" else 0
        rows = conn.execute(
            "SELECT * FROM signals WHERE traded = ? ORDER BY ts DESC", (val,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM signals ORDER BY ts DESC").fetchall()
    conn.close()

    results = []
    for r in rows:
        d = dict(r)
        if d.get("indicators"):
            try:
                d["indicators"] = json.loads(d["indicators"])
            except (json.JSONDecodeError, TypeError):
                pass
        results.append(d)
    return results


@app.get("/daily")
def daily_summary():
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=30)).isoformat()

    rows = conn.execute(
        """SELECT DATE(ex.ts) as date,
                  COUNT(*) as trades,
                  SUM(CASE WHEN ex.pnl > 0 THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN ex.pnl <= 0 THEN 1 ELSE 0 END) as losses,
                  ROUND(SUM(ex.pnl), 2) as pnl
           FROM exits ex
           WHERE ex.ts >= ?
           GROUP BY DATE(ex.ts)
           ORDER BY date DESC""",
        (cutoff,),
    ).fetchall()
    conn.close()
    return [DailyOut(**dict(r)) for r in rows]


@app.get("/health")
def health():
    conn = get_conn()
    sc = conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    ec = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    xc = conn.execute("SELECT COUNT(*) FROM exits").fetchone()[0]
    conn.close()
    return HealthOut(db_path=DB_PATH, signal_count=sc, entry_count=ec, exit_count=xc)
