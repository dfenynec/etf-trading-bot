import os
from dotenv import load_dotenv

load_dotenv()

# --- Alpaca credentials (loaded from .env) ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER_TRADING = True  # Switch to False ONLY when ready for real money

# --- Screener settings ---
# The full candidate pools live in screener.py (ETF_CANDIDATES, CRYPTO_CANDIDATES).
# These settings control how many the screener selects for active trading.
SCREEN_TOP_N_ETF    = 12          # Trade the top 12 ETFs by momentum (from ~35 candidates)
SCREEN_TOP_N_CRYPTO = 8           # Watch the top 8 crypto pairs by momentum (from 19 candidates)
SCREEN_MIN_VOLUME   = 500_000     # Skip ETFs with avg daily volume below this

# --- Technical indicator settings ---
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9

SMA_SHORT = 50
SMA_LONG = 200

BB_PERIOD = 20
BB_STD = 2

ATR_PERIOD = 14
STOCH_PERIOD = 14

# --- Risk management ---
MAX_POSITIONS = 5             # Max open long positions at once
MAX_SHORT_POSITIONS = 3       # Max open short positions at once
MAX_POSITION_PCT = 0.10       # Max 10% of portfolio per position (10% rule)
STOP_LOSS_ATR_MULT = 2.0      # Stop-loss = entry - 2 * ATR
TAKE_PROFIT_ATR_MULT = 3.0    # Take-profit = entry + 3 * ATR
# Percentage caps — prevent ATR from stretching stops/targets too far in volatile markets
STOP_LOSS_MAX_PCT  = 0.04     # Stop never more than 4% below entry
TAKE_PROFIT_MAX_PCT = 0.07    # Target never more than 7% above entry (achievable in calm markets)
RISK_PER_TRADE_PCT = 0.01     # Risk exactly 1% of portfolio per trade (position sized by stop distance)
MIN_BUY_SCORE = 4             # Minimum score to trigger a BUY (4+ indicators must agree)
MIN_SELL_SCORE = -4           # Maximum score to trigger a SELL (tightened to match)

# --- Crypto risk settings (separate from ETF — crypto is more volatile) ---
MAX_CRYPTO_POSITIONS = 2       # Max 2 concurrent crypto positions (limits correlated loss)
MAX_CRYPTO_POSITION_PCT = 0.08 # Max 8% per crypto position (lower — crypto is more volatile)
CRYPTO_RUN_INTERVAL_MINUTES = 30  # Check crypto every 30 minutes

# --- Risk circuit breakers ---
DAILY_LOSS_LIMIT_PCT  = 0.03  # Halt all new trades if daily P&L drops below -3%

# --- Trailing stop ---
# Once a position is open, the stop trails TRAILING_STOP_PCT below the highest
# price seen since entry.  It only moves UP — never down.
# Example: entry $82, price runs to $92 → trailing stop = $92 × (1 - 0.03) = $89.24
# If price then drops to $89.24 the position closes, locking in +$7.24 per unit.
TRAILING_STOP_PCT = 0.03      # Trail stop 3% below the running peak price

# --- Pyramiding (scaling into winners) ---
# When a position gains PYRAMID_TRIGGER_PCT, add PYRAMID_ADD_PCT of the original
# qty to the position.  The stop is moved to the original entry price (breakeven)
# so the add-on is free-rolled on top of a protected base.  Only done once per trade.
PYRAMID_TRIGGER_PCT = 0.03    # Add to position once it's up 3%
PYRAMID_ADD_PCT     = 0.50    # Add 50% of the original quantity

# --- Kelly Criterion (dynamic risk sizing) ---
# Uses win rate + avg win/loss from trade_journal.csv to size each trade
# for maximum geometric (compounding) growth.
# Falls back to RISK_PER_TRADE_PCT if fewer than KELLY_MIN_TRADES are recorded.
KELLY_MIN_TRADES  = 10        # Minimum closed trades before Kelly activates
KELLY_MIN_RISK    = 0.005     # Never risk less than 0.5% per trade
KELLY_MAX_RISK    = 0.02      # Never risk more than 2% per trade
KELLY_FRACTION    = 0.5       # Use half-Kelly (safer — full Kelly is too aggressive)

# --- Consecutive loss protection ---
# After LOSS_THROTTLE_AFTER consecutive losses, position size is halved.
# Resets to full size after the next winning trade.
LOSS_THROTTLE_AFTER = 2       # Halve size after this many consecutive losses

# --- Regime detection thresholds ---
ADX_RANGING_THRESHOLD  = 20   # ADX below this = ranging/choppy → mean-reversion mode
ADX_TRENDING_THRESHOLD = 25   # ADX above this = clearly trending → trend-following mode
# Zone 20–25 is transitional — treated conservatively as ranging

# --- Mean reversion settings (ranging markets only) ---
MR_RSI_OVERSOLD  = 30  # Deeper extreme needed to BUY in ranging market
MR_RSI_OVERBOUGHT = 70  # Deeper extreme needed to SELL in ranging market
MIN_MR_SCORE     = 3   # Score threshold for mean-reversion trades (max possible ~5)

# --- Correlation filter ---
BTC_CORRELATION_FILTER = True # Suppress altcoin BUYs when BTC score is negative

# --- Performance ---
POSITION_CACHE_TTL = 30       # Seconds to cache get_positions() (reduces API calls)

# --- Bot settings ---
LOOKBACK_DAYS = 300           # Days of history to fetch for indicators
RUN_INTERVAL_MINUTES = 30     # How often the ETF strategy runs (during market hours)
