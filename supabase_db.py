"""
Supabase backend for trade logger.

Why Supabase instead of SQLite?
- SQLite lives on the Mac mini only — if the machine dies, data is gone
- Supabase is cloud Postgres — accessible from anywhere (phone, Render, etc.)
- You can browse/query your trade data visually in the Supabase dashboard
- Already paid for — zero marginal cost

We use the service_role key (full access) because this is a server-side
process, never exposed to the browser.
"""
import os
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv(os.path.expanduser('~/Developer/shared/.env'))

SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY')  # server-side: use service role

_client: Client | None = None


def get_supabase() -> Client:
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set')
        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def init_tables():
    """
    Create tables in Supabase if they don't exist.
    
    We run raw SQL via the Postgres connection. Supabase exposes this
    through their SQL editor UI too — same tables you'd see there.
    """
    sb = get_supabase()
    
    # Create tables using RPC (raw SQL)
    # In Supabase, you can also do this via their SQL editor in the dashboard
    create_signals = """
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        ts TIMESTAMPTZ DEFAULT NOW(),
        source TEXT,
        ticker TEXT,
        direction TEXT,
        signal_score FLOAT DEFAULT 0,
        indicators JSONB,
        proposed BOOLEAN DEFAULT FALSE,
        traded BOOLEAN DEFAULT FALSE,
        skip_reason TEXT
    );
    """
    
    create_entries = """
    CREATE TABLE IF NOT EXISTS entries (
        id TEXT PRIMARY KEY,
        signal_id TEXT REFERENCES signals(id) ON DELETE SET NULL,
        ts TIMESTAMPTZ DEFAULT NOW(),
        broker TEXT,
        ticker TEXT,
        instrument TEXT,
        direction TEXT,
        strike FLOAT,
        expiry TEXT,
        contracts INTEGER DEFAULT 1,
        entry_price FLOAT,
        total_cost FLOAT,
        account_size_at_entry FLOAT,
        pct_of_account FLOAT
    );
    """
    
    create_exits = """
    CREATE TABLE IF NOT EXISTS exits (
        id TEXT PRIMARY KEY,
        entry_id TEXT REFERENCES entries(id) ON DELETE SET NULL,
        ts TIMESTAMPTZ DEFAULT NOW(),
        exit_price FLOAT,
        contracts INTEGER DEFAULT 1,
        pnl FLOAT,
        pnl_pct FLOAT,
        exit_reason TEXT,
        hold_time_min INTEGER
    );
    """
    
    for sql in [create_signals, create_entries, create_exits]:
        try:
            sb.rpc('exec_sql', {'sql': sql}).execute()
        except Exception:
            # RPC might not exist — tables may already exist or need manual creation
            pass


def insert_signal(data: dict) -> dict:
    sb = get_supabase()
    result = sb.table('signals').insert(data).execute()
    return result.data[0] if result.data else {}


def insert_entry(data: dict) -> dict:
    sb = get_supabase()
    result = sb.table('entries').insert(data).execute()
    return result.data[0] if result.data else {}


def insert_exit(data: dict) -> dict:
    sb = get_supabase()
    result = sb.table('exits').insert(data).execute()
    return result.data[0] if result.data else {}


def get_stats() -> dict:
    """Aggregate win rate, P&L, by ticker/broker from Supabase."""
    sb = get_supabase()
    
    exits = sb.table('exits').select('pnl, entry_id').execute().data or []
    entries = sb.table('entries').select('id, ticker, broker').execute().data or []
    
    entry_map = {e['id']: e for e in entries}
    
    pnls = [x['pnl'] for x in exits if x.get('pnl') is not None]
    if not pnls:
        return {'total_trades': 0, 'win_rate': 0, 'total_pnl': 0,
                'avg_pnl': 0, 'best_trade': 0, 'worst_trade': 0,
                'by_ticker': {}, 'by_broker': {}}
    
    wins = [p for p in pnls if p > 0]
    by_ticker: dict = {}
    by_broker: dict = {}
    
    for x in exits:
        entry = entry_map.get(x.get('entry_id'), {})
        pnl = x.get('pnl') or 0
        
        for key, bucket in [('ticker', by_ticker), ('broker', by_broker)]:
            val = entry.get(key, 'unknown')
            if val not in bucket:
                bucket[val] = {'trades': 0, 'pnl': 0.0, 'wins': 0}
            bucket[val]['trades'] += 1
            bucket[val]['pnl'] = round(bucket[val]['pnl'] + pnl, 2)
            if pnl > 0:
                bucket[val]['wins'] += 1
    
    for bucket in [by_ticker, by_broker]:
        for k in bucket:
            t = bucket[k]['trades']
            bucket[k]['win_rate'] = round((bucket[k]['wins'] / t) * 100, 1) if t else 0
    
    return {
        'total_trades': len(pnls),
        'win_rate': round((len(wins) / len(pnls)) * 100, 1),
        'avg_pnl': round(sum(pnls) / len(pnls), 2),
        'avg_win': round(sum(wins) / len(wins), 2) if wins else 0,
        'avg_loss': round(sum(p for p in pnls if p <= 0) / max(1, len(pnls) - len(wins)), 2),
        'best_trade': max(pnls),
        'worst_trade': min(pnls),
        'total_pnl': round(sum(pnls), 2),
        'by_ticker': by_ticker,
        'by_broker': by_broker,
    }


def get_signals(traded: bool | None = None) -> list:
    sb = get_supabase()
    q = sb.table('signals').select('*').order('ts', desc=True)
    if traded is not None:
        q = q.eq('traded', traded)
    return q.execute().data or []


def get_entries(ticker: str | None = None, broker: str | None = None, date: str | None = None) -> list:
    sb = get_supabase()
    q = sb.table('entries').select('*').order('ts', desc=True)
    if ticker:
        q = q.eq('ticker', ticker)
    if broker:
        q = q.eq('broker', broker)
    if date:
        q = q.gte('ts', f'{date}T00:00:00Z').lte('ts', f'{date}T23:59:59Z')
    return q.execute().data or []


def get_daily_summary(days: int = 30) -> list:
    """Get day-by-day P&L. Supabase doesn't have GROUP BY via the client lib,
    so we do it in Python. For large datasets, move this to a DB view."""
    sb = get_supabase()
    exits = sb.table('exits').select('ts, pnl').order('ts', desc=True).limit(1000).execute().data or []
    
    by_date: dict = {}
    for x in exits:
        if not x.get('ts') or x.get('pnl') is None:
            continue
        date = x['ts'][:10]  # YYYY-MM-DD
        if date not in by_date:
            by_date[date] = {'date': date, 'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
        by_date[date]['trades'] += 1
        by_date[date]['pnl'] = round(by_date[date]['pnl'] + x['pnl'], 2)
        if x['pnl'] > 0:
            by_date[date]['wins'] += 1
        else:
            by_date[date]['losses'] += 1
    
    return sorted(by_date.values(), key=lambda d: d['date'], reverse=True)[:days]
