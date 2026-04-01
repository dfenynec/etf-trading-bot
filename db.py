"""
Database layer — PostgreSQL via psycopg2.
Tables:
  trades       — every trade action (replaces trade_journal.csv)
  open_entries — crypto trailing stop state (replaces open_entries.json)

Falls back gracefully if DATABASE_URL is not set (CSV-only mode).
"""
import logging
import os

logger = logging.getLogger(__name__)

_conn = None


def get_conn():
    """Return a live psycopg2 connection, reconnecting if needed."""
    global _conn
    url = os.environ.get("DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg2
        if _conn is None or _conn.closed:
            _conn = psycopg2.connect(url, sslmode="require")
            _conn.autocommit = True
        return _conn
    except Exception as e:
        logger.error(f"[DB] Connection failed: {e}")
        return None


def init_db() -> bool:
    """Create tables if they don't exist. Returns True on success."""
    conn = get_conn()
    if not conn:
        logger.warning("[DB] DATABASE_URL not set — running in CSV-only mode")
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          SERIAL PRIMARY KEY,
                    ts          TIMESTAMPTZ DEFAULT NOW(),
                    action      VARCHAR(10),
                    symbol      VARCHAR(20),
                    qty         DOUBLE PRECISION,
                    price       DOUBLE PRECISION,
                    score       INTEGER,
                    stop        DOUBLE PRECISION,
                    take_profit DOUBLE PRECISION,
                    note        TEXT,
                    pnl_pct     DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS open_entries (
                    symbol       VARCHAR(20) PRIMARY KEY,
                    entry_price  DOUBLE PRECISION,
                    atr          DOUBLE PRECISION,
                    stop         DOUBLE PRECISION,
                    peak_price   DOUBLE PRECISION,
                    orig_qty     DOUBLE PRECISION,
                    pyramided    BOOLEAN DEFAULT FALSE,
                    trail_active BOOLEAN DEFAULT FALSE,
                    created_at   TIMESTAMPTZ DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS learned_weights (
                    id         INT PRIMARY KEY DEFAULT 1,
                    weights    TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS equity_snapshots (
                    id      SERIAL PRIMARY KEY,
                    ts      TIMESTAMPTZ DEFAULT NOW(),
                    equity  DOUBLE PRECISION,
                    cash    DOUBLE PRECISION
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots (ts);
            """)
        logger.info("[DB] Tables ready (trades, open_entries)")
        return True
    except Exception as e:
        logger.error(f"[DB] Table creation failed: {e}")
        return False


def insert_trade(action, symbol, qty, price, score=0,
                 stop=None, take_profit=None, note="", pnl_pct=None) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO trades (action, symbol, qty, price, score, stop, take_profit, note, pnl_pct)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (action, symbol, qty, price, score, stop, take_profit, note, pnl_pct))
        return True
    except Exception as e:
        logger.error(f"[DB] insert_trade failed: {e}")
        return False


def get_trades(limit: int = 30) -> list:
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, action, symbol, qty, price, score, stop, take_profit, note, pnl_pct
                FROM trades ORDER BY ts DESC LIMIT %s
            """, (limit,))
            cols = ["timestamp", "action", "symbol", "qty", "price",
                    "score", "stop", "take_profit", "note", "pnl_pct"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"[DB] get_trades failed: {e}")
        return []


def get_performance_stats() -> dict:
    """Calculate win rate, profit factor, avg win/loss from DB trades table."""
    conn = get_conn()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM trades WHERE action IN ('BUY','SHORT')")
            total_entries = cur.fetchone()[0]

            cur.execute("SELECT pnl_pct FROM trades WHERE action='CLOSE' AND pnl_pct IS NOT NULL")
            pnls = [row[0] for row in cur.fetchall()]

        if not pnls:
            return {"total_entries": total_entries, "completed": 0,
                    "message": "No closed trades yet"}

        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        win_rate      = len(wins) / len(pnls) * 100
        avg_win       = sum(wins)   / len(wins)   if wins   else 0.0
        avg_loss      = sum(losses) / len(losses) if losses else 0.0
        total_pnl     = sum(pnls)
        profit_factor = (
            abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0
            else float("inf")
        )
        return {
            "total_entries": total_entries,
            "completed":     len(pnls),
            "wins":          len(wins),
            "losses":        len(losses),
            "win_rate":      round(win_rate, 1),
            "avg_win_pct":   round(avg_win, 2),
            "avg_loss_pct":  round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "total_pnl_pct": round(total_pnl, 2),
        }
    except Exception as e:
        logger.error(f"[DB] get_performance_stats failed: {e}")
        return {}


def save_entry(alpaca_sym: str, entry: dict) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO open_entries
                    (symbol, entry_price, atr, stop, peak_price, orig_qty,
                     pyramided, trail_active, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (symbol) DO UPDATE SET
                    entry_price  = EXCLUDED.entry_price,
                    atr          = EXCLUDED.atr,
                    stop         = EXCLUDED.stop,
                    peak_price   = EXCLUDED.peak_price,
                    orig_qty     = EXCLUDED.orig_qty,
                    pyramided    = EXCLUDED.pyramided,
                    trail_active = EXCLUDED.trail_active,
                    updated_at   = NOW()
            """, (
                alpaca_sym,
                entry.get("price"),
                entry.get("atr"),
                entry.get("stop"),
                entry.get("peak_price"),
                entry.get("orig_qty"),
                entry.get("pyramided", False),
                entry.get("trail_active", False),
            ))
        return True
    except Exception as e:
        logger.error(f"[DB] save_entry failed for {alpaca_sym}: {e}")
        return False


def delete_entry(alpaca_sym: str) -> bool:
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM open_entries WHERE symbol = %s", (alpaca_sym,))
        return True
    except Exception as e:
        logger.error(f"[DB] delete_entry failed for {alpaca_sym}: {e}")
        return False


def save_weights(weights: dict) -> bool:
    """Upsert learned weights into DB (single persistent row)."""
    conn = get_conn()
    if not conn:
        return False
    try:
        import json as _json
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS learned_weights (
                    id          INT PRIMARY KEY DEFAULT 1,
                    weights     TEXT,
                    updated_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                INSERT INTO learned_weights (id, weights, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE SET
                    weights    = EXCLUDED.weights,
                    updated_at = NOW()
            """, (_json.dumps(weights),))
        return True
    except Exception as e:
        logger.error(f"[DB] save_weights failed: {e}")
        return False


def load_weights() -> dict:
    """Load learned weights from DB. Returns {} if not found."""
    conn = get_conn()
    if not conn:
        return {}
    try:
        import json as _json
        with conn.cursor() as cur:
            cur.execute("""
                SELECT weights FROM learned_weights WHERE id = 1
            """)
            row = cur.fetchone()
            if row:
                return _json.loads(row[0])
        return {}
    except Exception as e:
        logger.error(f"[DB] load_weights failed: {e}")
        return {}


def get_all_trades() -> list:
    """Return all trades from DB for learner analysis (no limit)."""
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, action, symbol, qty, price, score, note, pnl_pct
                FROM trades ORDER BY ts ASC
            """)
            cols = ["timestamp", "action", "symbol", "qty", "price", "score", "note", "pnl_pct"]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"[DB] get_all_trades failed: {e}")
        return []


def insert_equity_snapshot(equity: float, cash: float) -> bool:
    """Record a portfolio equity snapshot. Throttled: skips if last snapshot < 5 min ago."""
    conn = get_conn()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            # Throttle: only insert if last snapshot is older than 5 minutes
            cur.execute("""
                SELECT ts FROM equity_snapshots ORDER BY ts DESC LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                from datetime import datetime, timezone, timedelta
                last_ts = row[0]
                if last_ts.tzinfo is None:
                    from datetime import timezone as tz
                    last_ts = last_ts.replace(tzinfo=tz.utc)
                now = datetime.now(timezone.utc)
                if (now - last_ts) < timedelta(minutes=5):
                    return False  # Too soon

            cur.execute("""
                INSERT INTO equity_snapshots (equity, cash) VALUES (%s, %s)
            """, (equity, cash))
        return True
    except Exception as e:
        logger.error(f"[DB] insert_equity_snapshot failed: {e}")
        return False


def get_equity_history(limit: int = 500) -> list:
    """Return equity snapshots ordered by time ASC."""
    conn = get_conn()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ts, equity, cash FROM equity_snapshots
                ORDER BY ts ASC LIMIT %s
            """, (limit,))
            return [{"ts": row[0].isoformat(), "equity": row[1], "cash": row[2]}
                    for row in cur.fetchall()]
    except Exception as e:
        logger.error(f"[DB] get_equity_history failed: {e}")
        return []


def load_all_entries() -> dict:
    """Load all open entries from DB. Returns {} if DB unavailable."""
    conn = get_conn()
    if not conn:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT symbol, entry_price, atr, stop, peak_price,
                       orig_qty, pyramided, trail_active
                FROM open_entries
            """)
            entries = {}
            for row in cur.fetchall():
                sym = row[0]
                entries[sym] = {
                    "price":        row[1],
                    "atr":          row[2],
                    "stop":         row[3],
                    "peak_price":   row[4],
                    "orig_qty":     row[5],
                    "pyramided":    row[6],
                    "trail_active": row[7],
                }
            if entries:
                logger.info(f"[DB] Restored {len(entries)} open entries: {list(entries.keys())}")
            return entries
    except Exception as e:
        logger.error(f"[DB] load_all_entries failed: {e}")
        return {}
