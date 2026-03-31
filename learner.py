"""
Adaptive Learning Engine — the bot's self-improvement system.

Learns from every closed trade to discover which indicators, regimes, and
symbols actually make money, then adjusts future decisions accordingly.

Three learning systems:
  1. Indicator weights    — amplify indicators that predict winners, suppress losers
  2. Regime effectiveness — track win rate per regime, skip underperforming regimes
  3. Symbol performance   — blacklist symbols that consistently lose money

All learning is gradual and capped to prevent overfitting on small samples.
Requires a minimum number of observations before adjusting any parameter.

Data source: PostgreSQL trades table (primary), trade_journal.csv (fallback)
Learned weights persisted to DB so they survive restarts.
"""
import csv
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)

JOURNAL_FILE  = "trade_journal.csv"
WEIGHTS_FILE  = "learned_weights.json"

# Minimum observations before learning kicks in
MIN_TRADES_FOR_INDICATOR = 5    # 5 trades where an indicator fired
MIN_TRADES_FOR_REGIME    = 8    # 8 closed trades in a regime
MIN_TRADES_FOR_SYMBOL    = 5    # 5 closed trades on a symbol

# Weight bounds (prevents runaway over/under-fitting)
MIN_WEIGHT = 0.3    # Never suppress an indicator below 30% of its normal contribution
MAX_WEIGHT = 2.0    # Never amplify above 200%

# Default weights file (regenerated if missing)
DEFAULT_WEIGHTS = {
    "indicator_weights": {},
    "regime_weights": {
        "TRENDING":  1.0,
        "RANGING":   1.0,
        "BREAKOUT":  1.0,
    },
    "symbol_blacklist": [],
    "meta": {
        "total_trades_analyzed": 0,
        "last_updated": 0,
    }
}

# ---------------------------------------------------------------------------
# Indicator name normalization
# ---------------------------------------------------------------------------

# Maps reason substrings → canonical indicator names
_INDICATOR_PATTERNS = [
    ("RSI oversold",          "rsi"),
    ("RSI overbought",        "rsi"),
    ("RSI deeply oversold",   "rsi"),
    ("RSI deeply overbought", "rsi"),
    ("RSI neutral",           "rsi"),
    ("RSI not overbought",    "rsi"),
    ("MACD bullish crossover","macd"),
    ("MACD bearish crossover","macd"),
    ("MACD above signal",     "macd"),
    ("MACD below signal",     "macd"),
    ("MACD bullish",          "macd"),
    ("MACD bearish",          "macd"),
    ("BB lower band",         "bollinger"),
    ("BB upper band",         "bollinger"),
    ("BB mid",                "bollinger"),
    ("Stochastic oversold",   "stochastic"),
    ("Stochastic overbought", "stochastic"),
    ("Stochastic neutral",    "stochastic"),
    ("EMA 9/21",              "ema_cross"),
    ("EMA 9 above",           "ema_cross"),
    ("EMA 9 below",           "ema_cross"),
    ("OBV rising",            "obv"),
    ("OBV falling",           "obv"),
    ("OBV neutral",           "obv"),
    ("Supertrend: BULLISH",   "supertrend"),
    ("Supertrend: BEARISH",   "supertrend"),
    ("volume confirms",       "volume"),
    ("Volume surge",          "volume"),
    ("Volume spike",          "volume"),
    ("Breakout above",        "breakout"),
]


def _extract_indicators(reasons_str: str) -> set:
    """Extract canonical indicator names from a reasons string."""
    found = set()
    for pattern, name in _INDICATOR_PATTERNS:
        if pattern.lower() in reasons_str.lower():
            found.add(name)
    return found


def _parse_pnl(note: str) -> float | None:
    """Extract P&L % from note like 'Trail stop | entry=$82.00 | pnl=+3.45%'"""
    match = re.search(r"pnl=([+-]?\d+\.?\d*)%", note)
    return float(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def _read_journal() -> list[dict]:
    """Read all trades from DB (primary) or CSV fallback."""
    import db as _db
    rows = _db.get_all_trades()
    if rows:
        return rows
    # CSV fallback
    if not os.path.exists(JOURNAL_FILE):
        return []
    rows = []
    try:
        with open(JOURNAL_FILE, newline="") as f:
            for row in csv.DictReader(f):
                rows.append(row)
    except Exception as e:
        logger.error(f"[LEARNER] Failed to read CSV journal: {e}")
    return rows


def _build_trade_pairs(rows: list[dict]) -> list[dict]:
    """
    Match BUY entries with their CLOSE exits.
    Returns a list of completed trades with entry info + exit P&L.
    """
    # Track open entries by symbol
    open_entries: dict = {}
    completed   = []

    for row in rows:
        action = row.get("action", "")
        symbol = row.get("symbol", "")

        if action == "BUY":
            open_entries[symbol] = {
                "symbol":    symbol,
                "score":     int(row.get("score", 0)),
                "note":      row.get("note", ""),
                "reasons":   row.get("note", ""),  # reasons stored in note for pyramid
            }
        elif action == "CLOSE" and symbol in open_entries:
            pnl = _parse_pnl(row.get("note", ""))
            if pnl is not None:
                entry = open_entries.pop(symbol)
                entry["pnl"]       = pnl
                entry["exit_note"] = row.get("note", "")
                entry["won"]       = pnl > 0
                completed.append(entry)

    return completed


def analyze() -> dict:
    """
    Full analysis of trading history. Returns learned weights dict.

    This is the bot's brain — it figures out what's working and what isn't.
    """
    rows = _read_journal()
    if not rows:
        return DEFAULT_WEIGHTS.copy()

    trades = _build_trade_pairs(rows)
    if len(trades) < 3:
        logger.info(f"[LEARNER] Only {len(trades)} completed trades — using defaults")
        return DEFAULT_WEIGHTS.copy()

    # ---- 1. Indicator effectiveness ----
    # For each indicator, track how many winning vs losing trades it appeared in
    indicator_stats: dict = {}  # name → {"wins": int, "losses": int}

    for trade in trades:
        # Try to find the original BUY's reasons from the note field
        # We also look at all BUY rows for this symbol to get the reasons
        indicators = set()
        for row in rows:
            if row.get("action") == "BUY" and row.get("symbol") == trade["symbol"]:
                indicators.update(_extract_indicators(row.get("note", "")))

        for ind in indicators:
            if ind not in indicator_stats:
                indicator_stats[ind] = {"wins": 0, "losses": 0}
            if trade["won"]:
                indicator_stats[ind]["wins"] += 1
            else:
                indicator_stats[ind]["losses"] += 1

    indicator_weights = {}
    for ind, stats in indicator_stats.items():
        total = stats["wins"] + stats["losses"]
        if total < MIN_TRADES_FOR_INDICATOR:
            indicator_weights[ind] = 1.0  # Not enough data — neutral
            continue
        win_rate = stats["wins"] / total
        # Weight = win_rate mapped to [MIN_WEIGHT, MAX_WEIGHT]
        # 50% win rate → 1.0 (neutral)
        # 70% win rate → 1.6
        # 30% win rate → 0.4
        weight = MIN_WEIGHT + (MAX_WEIGHT - MIN_WEIGHT) * win_rate
        indicator_weights[ind] = round(weight, 3)

    # ---- 2. Regime effectiveness ----
    regime_stats: dict = {}  # regime → {"wins": int, "losses": int}

    for trade in trades:
        # Detect regime from note
        for regime in ["TRENDING", "RANGING", "BREAKOUT"]:
            if regime in trade.get("exit_note", "") or regime in trade.get("reasons", ""):
                if regime not in regime_stats:
                    regime_stats[regime] = {"wins": 0, "losses": 0}
                if trade["won"]:
                    regime_stats[regime]["wins"] += 1
                else:
                    regime_stats[regime]["losses"] += 1
                break

    regime_weights = {"TRENDING": 1.0, "RANGING": 1.0, "BREAKOUT": 1.0}
    for regime, stats in regime_stats.items():
        total = stats["wins"] + stats["losses"]
        if total < MIN_TRADES_FOR_REGIME:
            continue
        win_rate = stats["wins"] / total
        # Below 35% win rate → reduce threshold (make entries harder)
        # Above 55% win rate → slightly boost
        regime_weights[regime] = round(
            max(0.5, min(1.5, 0.5 + win_rate)), 3
        )

    # ---- 3. Symbol blacklist ----
    symbol_stats: dict = {}  # symbol → {"wins": int, "losses": int, "total_pnl": float}

    for trade in trades:
        sym = trade["symbol"]
        if sym not in symbol_stats:
            symbol_stats[sym] = {"wins": 0, "losses": 0, "total_pnl": 0.0}
        if trade["won"]:
            symbol_stats[sym]["wins"] += 1
        else:
            symbol_stats[sym]["losses"] += 1
        symbol_stats[sym]["total_pnl"] += trade["pnl"]

    blacklist = []
    for sym, stats in symbol_stats.items():
        total = stats["wins"] + stats["losses"]
        if total < MIN_TRADES_FOR_SYMBOL:
            continue
        win_rate = stats["wins"] / total
        # Blacklist: <30% win rate AND total P&L negative after 5+ trades
        if win_rate < 0.30 and stats["total_pnl"] < 0:
            blacklist.append(sym)
            logger.info(
                f"[LEARNER] Blacklisting {sym}: {win_rate*100:.0f}% win rate, "
                f"{stats['total_pnl']:+.2f}% total P&L over {total} trades"
            )

    # ---- 4. Time-of-day analysis (crypto only) ----
    # Track win rate by hour UTC. Hours with <35% win rate (min 3 trades) are flagged.
    hour_stats: dict = {}  # hour → {"wins": int, "losses": int}
    for trade in trades:
        ts_str = trade.get("timestamp") or trade.get("ts", "")
        try:
            ts_str = str(ts_str)
            # Extract hour from "2026-03-30 02:20:00" or datetime object
            hour = int(ts_str[11:13]) if len(ts_str) >= 13 else None
        except Exception:
            hour = None
        if hour is None:
            continue
        if hour not in hour_stats:
            hour_stats[hour] = {"wins": 0, "losses": 0}
        if trade["won"]:
            hour_stats[hour]["wins"] += 1
        else:
            hour_stats[hour]["losses"] += 1

    bad_hours = []
    for hour, stats in hour_stats.items():
        total = stats["wins"] + stats["losses"]
        if total >= 3:
            win_rate = stats["wins"] / total
            if win_rate < 0.35:
                bad_hours.append(hour)
                logger.info(
                    f"[LEARNER] Flagged bad hour UTC {hour:02d}:00 — "
                    f"{win_rate*100:.0f}% win rate over {total} trades"
                )

    result = {
        "indicator_weights": indicator_weights,
        "regime_weights":    regime_weights,
        "symbol_blacklist":  blacklist,
        "bad_hours_utc":     bad_hours,
        "meta": {
            "total_trades_analyzed": len(trades),
            "last_updated": time.time(),
        }
    }

    # Persist to DB (primary) and JSON file (fallback)
    import db as _db
    _db.save_weights(result)
    try:
        with open(WEIGHTS_FILE, "w") as f:
            json.dump(result, f, indent=2)
    except Exception as e:
        logger.error(f"[LEARNER] Failed to save weights JSON: {e}")

    return result


# ---------------------------------------------------------------------------
# Public interface (used by strategy.py and live_trader.py)
# ---------------------------------------------------------------------------

_cached_weights = None
_cache_time     = 0.0
REFRESH_INTERVAL = 1800  # Re-analyze every 30 min


def get_weights() -> dict:
    """
    Return current learned weights, refreshed every 30 min.
    On first call, loads from DB so previous learning survives restarts.
    """
    global _cached_weights, _cache_time

    if _cached_weights and time.time() - _cache_time < REFRESH_INTERVAL:
        return _cached_weights

    # First call: try loading persisted weights from DB before re-analyzing
    if _cached_weights is None:
        import db as _db
        persisted = _db.load_weights()
        if persisted and persisted.get("meta", {}).get("total_trades_analyzed", 0) > 0:
            logger.info(
                f"[LEARNER] Loaded weights from DB "
                f"({persisted['meta']['total_trades_analyzed']} trades analyzed)"
            )
            _cached_weights = persisted
            _cache_time     = time.time()
            return _cached_weights

    _cached_weights = analyze()
    _cache_time     = time.time()
    return _cached_weights


def get_indicator_weight(indicator_name: str) -> float:
    """Get the learned weight for a specific indicator. Default: 1.0."""
    weights = get_weights()
    return weights.get("indicator_weights", {}).get(indicator_name, 1.0)


def get_regime_weight(regime: str) -> float:
    """Get the learned effectiveness weight for a regime. Default: 1.0."""
    weights = get_weights()
    return weights.get("regime_weights", {}).get(regime, 1.0)


def is_symbol_blacklisted(symbol: str) -> bool:
    """Check if a symbol has been blacklisted due to poor performance."""
    weights = get_weights()
    return symbol in weights.get("symbol_blacklist", [])


def print_learned_state() -> None:
    """Log the current learned state for visibility in Railway logs."""
    w = get_weights()
    meta = w.get("meta", {})

    if meta.get("total_trades_analyzed", 0) == 0:
        logger.info("[LEARNER] No completed trades to learn from yet — using default weights")
        return

    logger.info("=" * 55)
    logger.info("  LEARNED WEIGHTS")
    logger.info("=" * 55)
    logger.info(f"  Trades analyzed: {meta.get('total_trades_analyzed', 0)}")

    ind_w = w.get("indicator_weights", {})
    if ind_w:
        logger.info("  Indicator weights:")
        for name, weight in sorted(ind_w.items(), key=lambda x: x[1], reverse=True):
            status = "★" if weight > 1.2 else ("▼" if weight < 0.8 else "·")
            logger.info(f"    {status} {name:<14} {weight:.3f}")

    reg_w = w.get("regime_weights", {})
    if any(v != 1.0 for v in reg_w.values()):
        logger.info("  Regime weights:")
        for regime, weight in reg_w.items():
            logger.info(f"    {regime:<12} {weight:.3f}")

    bl = w.get("symbol_blacklist", [])
    if bl:
        logger.info(f"  Blacklisted symbols: {', '.join(bl)}")

    logger.info("=" * 55)
