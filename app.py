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
from fastapi.responses import JSONResponse, HTMLResponse

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


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """
    Quick HTML P&L dashboard. No auth needed (localhost only).

    Why HTML and not JSON: Saj wants a glanceable view in a browser tab.
    The /stats and /daily endpoints already serve the JSON — this wraps it
    in something human-readable without needing a full frontend.
    """
    try:
        s = stats()
        daily_data = daily(days=14)
        # Fetch signal data directly (avoid FastAPI Query wrapper issue when calling internally)
        if _use_supabase:
            from supabase_db import get_supabase
            signal_data = get_supabase().table('signals').select('*').order('ts', desc=True).execute().data or []
        else:
            from database import get_conn
            conn = get_conn()
            signal_data = [dict(r) for r in conn.execute('SELECT * FROM signals ORDER BY ts DESC').fetchall()]
            conn.close()
        # signals() returns a list from Supabase or SQLite
        if isinstance(signal_data, list):
            sig_total = len(signal_data)
            sig_traded = sum(1 for x in signal_data if x.get('traded'))
            sig_proposed = sum(1 for x in signal_data if x.get('proposed'))
            # group by source
            sources: dict = {}
            for x in signal_data:
                src = x.get('source', 'unknown')
                sources[src] = sources.get(src, 0) + 1
        else:
            sig_total = sig_traded = sig_proposed = 0
            sources = {}

        if isinstance(s, dict):
            total_pnl = s.get('total_pnl', 0)
            win_rate = s.get('win_rate', 0)
            total_trades = s.get('total_trades', 0)
            avg_pnl = s.get('avg_pnl', 0)
            best_trade = s.get('best_trade', 0)
            worst_trade = s.get('worst_trade', 0)
            by_ticker = s.get('by_ticker', {})
        else:
            total_pnl = getattr(s, 'total_pnl', 0)
            win_rate = getattr(s, 'win_rate', 0)
            total_trades = getattr(s, 'total_trades', 0)
            avg_pnl = getattr(s, 'avg_pnl', 0)
            best_trade = getattr(s, 'best_trade', 0)
            worst_trade = getattr(s, 'worst_trade', 0)
            by_ticker = getattr(s, 'by_ticker', {})

        # P&L color
        pnl_color = '#00d26a' if total_pnl >= 0 else '#ff4444'

        # Daily rows HTML
        daily_rows = ''
        for d in (daily_data if isinstance(daily_data, list) else []):
            date_str = d.get('date', '')
            dpnl = d.get('pnl', 0) or 0
            dpnl_color = '#00d26a' if dpnl >= 0 else '#ff4444'
            wins = d.get('wins', 0)
            losses = d.get('losses', 0)
            trades = d.get('trades', 0)
            wr = round(wins / trades * 100, 1) if trades > 0 else 0
            daily_rows += f"""
            <tr>
                <td>{date_str}</td>
                <td>{trades}</td>
                <td>{wins}W / {losses}L</td>
                <td>{wr:.1f}%</td>
                <td style="color:{dpnl_color};font-weight:600">${dpnl:+.2f}</td>
            </tr>"""

        # Ticker breakdown rows
        ticker_rows = ''
        if isinstance(by_ticker, dict):
            for ticker_name, td in sorted(by_ticker.items(), key=lambda x: -x[1].get('pnl', 0)):
                td_pnl = td.get('pnl', 0)
                td_color = '#00d26a' if td_pnl >= 0 else '#ff4444'
                ticker_rows += f"""
                <tr>
                    <td><strong>{ticker_name}</strong></td>
                    <td>{td.get('trades', 0)}</td>
                    <td>{td.get('wins', 0)}</td>
                    <td>{td.get('win_rate', 0):.1f}%</td>
                    <td style="color:{td_color}">${td_pnl:+.2f}</td>
                </tr>"""

        # Signal source breakdown
        source_rows = ''
        for src, count in sources.items():
            source_rows += f"<tr><td>{src}</td><td>{count}</td></tr>"

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trade Logger Dashboard</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         background: #0f0f13; color: #e8e8e8; margin: 0; padding: 20px; }}
  h1 {{ font-size: 1.4rem; font-weight: 700; margin: 0 0 4px; }}
  .subtitle {{ color: #888; font-size: 0.85rem; margin-bottom: 24px; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 28px; }}
  .card {{ background: #1a1a24; border: 1px solid #2a2a3a; border-radius: 10px;
           padding: 16px 20px; min-width: 140px; flex: 1; }}
  .card-label {{ font-size: 0.72rem; color: #888; text-transform: uppercase;
                 letter-spacing: 0.05em; margin-bottom: 6px; }}
  .card-value {{ font-size: 1.6rem; font-weight: 700; }}
  h2 {{ font-size: 1rem; font-weight: 600; color: #aaa; margin: 20px 0 10px; text-transform: uppercase;
        letter-spacing: 0.05em; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.875rem; margin-bottom: 28px; }}
  th {{ text-align: left; color: #888; font-weight: 500; padding: 8px 10px;
        border-bottom: 1px solid #2a2a3a; font-size: 0.75rem; text-transform: uppercase; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #1e1e2a; }}
  tr:hover td {{ background: #1e1e2a; }}
  .badge {{ background: #2a2a3a; border-radius: 4px; padding: 2px 8px; font-size: 0.75rem; }}
  .ts {{ color: #555; font-size: 0.75rem; margin-top: 16px; }}
</style>
</head>
<body>
<h1>📊 Trade Logger</h1>
<p class="subtitle">Central trade analytics — all brokers, all signals · <a href="/docs" style="color:#6b8cff">API Docs</a></p>

<div class="cards">
  <div class="card">
    <div class="card-label">Total P&L</div>
    <div class="card-value" style="color:{pnl_color}">${total_pnl:+.2f}</div>
  </div>
  <div class="card">
    <div class="card-label">Win Rate</div>
    <div class="card-value">{win_rate:.1f}%</div>
  </div>
  <div class="card">
    <div class="card-label">Total Trades</div>
    <div class="card-value">{total_trades}</div>
  </div>
  <div class="card">
    <div class="card-label">Avg P&L</div>
    <div class="card-value" style="color:{'#00d26a' if avg_pnl >= 0 else '#ff4444'}">${avg_pnl:+.2f}</div>
  </div>
  <div class="card">
    <div class="card-label">Best Trade</div>
    <div class="card-value" style="color:#00d26a">${best_trade:+.2f}</div>
  </div>
  <div class="card">
    <div class="card-label">Worst Trade</div>
    <div class="card-value" style="color:#ff4444">${worst_trade:+.2f}</div>
  </div>
</div>

<h2>Signal Intelligence</h2>
<div class="cards">
  <div class="card">
    <div class="card-label">Total Signals</div>
    <div class="card-value">{sig_total}</div>
  </div>
  <div class="card">
    <div class="card-label">Proposed</div>
    <div class="card-value">{sig_proposed}</div>
  </div>
  <div class="card">
    <div class="card-label">Traded</div>
    <div class="card-value">{sig_traded}</div>
  </div>
  <div class="card">
    <div class="card-label">Signal→Trade Rate</div>
    <div class="card-value">{'N/A' if sig_total == 0 else f"{sig_traded/sig_total*100:.1f}%"}</div>
  </div>
</div>

{'<h2>Signal Sources</h2><table><thead><tr><th>Source</th><th>Signals</th></tr></thead><tbody>' + source_rows + '</tbody></table>' if source_rows else ''}

<h2>By Ticker</h2>
<table>
  <thead><tr><th>Ticker</th><th>Trades</th><th>Wins</th><th>Win Rate</th><th>P&L</th></tr></thead>
  <tbody>{ticker_rows if ticker_rows else '<tr><td colspan="5" style="color:#555">No trades yet</td></tr>'}</tbody>
</table>

<h2>Daily Summary (Last 14 Days)</h2>
<table>
  <thead><tr><th>Date</th><th>Trades</th><th>W/L</th><th>Win Rate</th><th>P&L</th></tr></thead>
  <tbody>{daily_rows if daily_rows else '<tr><td colspan="5" style="color:#555">No data yet</td></tr>'}</tbody>
</table>

<p class="ts">Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')} · Backend: {'Supabase' if _use_supabase else 'SQLite'}</p>
</body></html>"""
        return HTMLResponse(content=html)
    except Exception as e:
        logger.exception('Dashboard error')
        return HTMLResponse(content=f'<h1>Error</h1><pre>{e}</pre>', status_code=500)


if __name__ == '__main__':
    import uvicorn
    port = int(__import__('os').getenv('PORT', '8010'))
    uvicorn.run('app:app', host='0.0.0.0', port=port, reload=False)
