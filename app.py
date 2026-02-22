"""
Trade Logger — FastAPI service on port 8010

Architecture decision: Supabase (cloud Postgres) as primary store,
SQLite as local fallback if Supabase is unreachable.

Why this matters:
- Supabase = accessible from anywhere, visual dashboard, cloud backup
- SQLite fallback = trading never stops logging even if network is down
- FastAPI = async, fast, auto-generates /docs with interactive API explorer

Ports/services:
- This service: 8010
- Called by: IBKR exit bot, TV webhook, options flow scanner, tradier bot
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from models import SignalIn, EntryIn, ExitIn, HealthOut, StatsOut, TradeOut, DailyOut

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

app = FastAPI(title="Trade Logger", version="2.0.0",
              description="Central trade logging service — signals, entries, exits across all brokers")

# Try Supabase first, fall back to SQLite
_use_supabase = False
_supabase = None

try:
    from supabase_db import get_supabase, insert_signal, insert_entry, insert_exit
    from supabase_db import get_stats, get_signals, get_entries, get_daily_summary
    sb = get_supabase()
    # Quick connectivity check
    sb.table('signals').select('id').limit(1).execute()
    _use_supabase = True
    logger.info('✅ Supabase connected — using cloud storage')
except Exception as e:
    logger.warning(f'⚠️  Supabase unavailable ({e}), falling back to SQLite')
    from database import init_db, get_conn


@app.on_event("startup")
def startup():
    global _use_supabase
    if not _use_supabase:
        from database import init_db
        init_db()
        logger.info('SQLite fallback initialized')
    else:
        logger.info('Trade Logger started — Supabase backend active')


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    if _use_supabase:
        try:
            sc = len(get_supabase().table('signals').select('id', count='exact').execute().data or [])
            ec = len(get_supabase().table('entries').select('id', count='exact').execute().data or [])
            xc = len(get_supabase().table('exits').select('id', count='exact').execute().data or [])
        except Exception:
            sc = ec = xc = 0
        return {'ok': True, 'backend': 'supabase',
                'url': 'https://yfqoomuensqcztdfmpyi.supabase.co',
                'signal_count': sc, 'entry_count': ec, 'exit_count': xc}
    else:
        from database import get_conn, DB_PATH
        conn = get_conn()
        sc = conn.execute('SELECT COUNT(*) FROM signals').fetchone()[0]
        ec = conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        xc = conn.execute('SELECT COUNT(*) FROM exits').fetchone()[0]
        conn.close()
        return HealthOut(db_path=DB_PATH, signal_count=sc, entry_count=ec, exit_count=xc)


# ── POST endpoints ────────────────────────────────────────────────────────────

@app.post("/signal")
def log_signal(s: SignalIn):
    ts = s.ts or datetime.utcnow()
    data = {
        'id': s.id,
        'ts': ts.isoformat(),
        'source': s.source,
        'ticker': s.ticker,
        'direction': s.direction,
        'signal_score': s.signal_score,
        'indicators': s.indicators if isinstance(s.indicators, dict) else
                      (json.loads(s.indicators) if s.indicators else None),
        'proposed': s.proposed,
        'traded': s.traded,
        'skip_reason': s.skip_reason,
    }

    if _use_supabase:
        insert_signal(data)
    else:
        conn = get_conn()
        conn.execute(
            """INSERT INTO signals (id, ts, source, ticker, direction, signal_score,
               indicators, proposed, traded, skip_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (data['id'], data['ts'], data['source'], data['ticker'], data['direction'],
             data['signal_score'], json.dumps(data['indicators']) if data['indicators'] else None,
             int(data['proposed']), int(data['traded']), data['skip_reason'])
        )
        conn.commit()
        conn.close()

    return {'ok': True, 'id': s.id}


@app.post("/entry")
def log_entry(e: EntryIn):
    ts = e.ts or datetime.utcnow()
    total_cost = e.total_cost or (e.entry_price * e.contracts)
    pct_of_account = round((total_cost / e.account_size_at_entry) * 100, 2) if e.account_size_at_entry else None

    data = {
        'id': e.id,
        'signal_id': e.signal_id,
        'ts': ts.isoformat(),
        'broker': e.broker,
        'ticker': e.ticker,
        'instrument': e.instrument,
        'direction': e.direction,
        'strike': e.strike,
        'expiry': e.expiry,
        'contracts': e.contracts,
        'entry_price': e.entry_price,
        'total_cost': total_cost,
        'account_size_at_entry': e.account_size_at_entry,
        'pct_of_account': pct_of_account,
    }

    if _use_supabase:
        insert_entry(data)
    else:
        conn = get_conn()
        conn.execute(
            """INSERT INTO entries (id, signal_id, ts, broker, ticker, instrument,
               direction, strike, expiry, contracts, entry_price, total_cost,
               account_size_at_entry, pct_of_account)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(data.values())
        )
        conn.commit()
        conn.close()

    return {'ok': True, 'id': e.id}


@app.post("/exit")
def log_exit(x: ExitIn):
    ts = x.ts or datetime.utcnow()

    # Auto-calc pnl_pct if not provided
    pnl_pct = x.pnl_pct
    if pnl_pct is None and x.entry_id:
        try:
            if _use_supabase:
                rows = get_supabase().table('entries').select('entry_price').eq('id', x.entry_id).execute().data
                if rows and rows[0]['entry_price']:
                    ep = rows[0]['entry_price']
                    pnl_pct = round(((x.exit_price - ep) / ep) * 100, 2)
            else:
                conn = get_conn()
                row = conn.execute('SELECT entry_price FROM entries WHERE id = ?', (x.entry_id,)).fetchone()
                if row and row['entry_price']:
                    ep = row['entry_price']
                    pnl_pct = round(((x.exit_price - ep) / ep) * 100, 2)
                conn.close()
        except Exception:
            pass

    data = {
        'id': x.id,
        'entry_id': x.entry_id,
        'ts': ts.isoformat(),
        'exit_price': x.exit_price,
        'contracts': x.contracts,
        'pnl': x.pnl,
        'pnl_pct': pnl_pct,
        'exit_reason': x.exit_reason,
        'hold_time_min': x.hold_time_min,
    }

    if _use_supabase:
        insert_exit(data)
    else:
        conn = get_conn()
        conn.execute(
            """INSERT INTO exits (id, entry_id, ts, exit_price, contracts, pnl,
               pnl_pct, exit_reason, hold_time_min)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            tuple(data.values())
        )
        conn.commit()
        conn.close()

    return {'ok': True, 'id': x.id}


# ── GET endpoints ─────────────────────────────────────────────────────────────

@app.get("/stats")
def stats():
    if _use_supabase:
        return get_stats()
    # SQLite fallback
    from database import get_conn
    conn = get_conn()
    exits = conn.execute('SELECT ex.pnl, ex.entry_id, e.ticker, e.broker FROM exits ex LEFT JOIN entries e ON ex.entry_id = e.id').fetchall()
    conn.close()
    if not exits:
        return StatsOut()
    pnls = [r['pnl'] for r in exits if r['pnl'] is not None]
    if not pnls:
        return StatsOut()
    wins = [p for p in pnls if p > 0]
    return StatsOut(
        total_trades=len(pnls),
        win_rate=round(len(wins)/len(pnls)*100, 1),
        avg_pnl=round(sum(pnls)/len(pnls), 2),
        best_trade=max(pnls),
        worst_trade=min(pnls),
        total_pnl=round(sum(pnls), 2),
    )


@app.get("/signals")
def signals(traded: Optional[str] = Query(None)):
    if _use_supabase:
        t = None if traded is None else (traded.lower() == 'true')
        return get_signals(traded=t)
    from database import get_conn
    conn = get_conn()
    if traded is not None:
        val = 1 if traded.lower() == 'true' else 0
        rows = conn.execute('SELECT * FROM signals WHERE traded = ? ORDER BY ts DESC', (val,)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM signals ORDER BY ts DESC').fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/trades")
def trades(ticker: Optional[str] = None, broker: Optional[str] = None, date: Optional[str] = None):
    if _use_supabase:
        entries = get_entries(ticker=ticker, broker=broker, date=date)
        sb = get_supabase()
        for entry in entries:
            entry['exits'] = sb.table('exits').select('*').eq('entry_id', entry['id']).execute().data or []
        return entries
    from database import get_conn
    conn = get_conn()
    where, params = [], []
    if ticker:
        where.append('e.ticker = ?')
        params.append(ticker)
    if broker:
        where.append('e.broker = ?')
        params.append(broker)
    clause = (' WHERE ' + ' AND '.join(where)) if where else ''
    rows = conn.execute(f'SELECT * FROM entries e{clause} ORDER BY e.ts DESC', params).fetchall()
    result = []
    for r in rows:
        entry = dict(r)
        exits = conn.execute('SELECT * FROM exits WHERE entry_id = ?', (entry['id'],)).fetchall()
        entry['exits'] = [dict(ex) for ex in exits]
        result.append(entry)
    conn.close()
    return result


@app.get("/daily")
def daily(days: int = 30):
    if _use_supabase:
        return get_daily_summary(days=days)
    from database import get_conn
    from datetime import timedelta
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT DATE(ex.ts) as date, COUNT(*) as trades,
           SUM(CASE WHEN ex.pnl > 0 THEN 1 ELSE 0 END) as wins,
           SUM(CASE WHEN ex.pnl <= 0 THEN 1 ELSE 0 END) as losses,
           ROUND(SUM(ex.pnl), 2) as pnl
           FROM exits ex WHERE ex.ts >= ? GROUP BY DATE(ex.ts) ORDER BY date DESC""",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


if __name__ == '__main__':
    import uvicorn
    port = int(__import__('os').getenv('PORT', '8010'))
    uvicorn.run('app:app', host='0.0.0.0', port=port, reload=False)
