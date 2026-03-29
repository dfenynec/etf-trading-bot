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
STOP_LOSS_ATR_MULT = 3.0      # Stop-loss = entry - 3 * ATR (wider — survives normal crypto noise)
TAKE_PROFIT_ATR_MULT = 4.0    # Take-profit = entry + 4 * ATR (keep 4:3 reward/risk)
MIN_BUY_SCORE = 4             # Minimum score to trigger a BUY (4+ indicators must agree)
MIN_SELL_SCORE = -4           # Maximum score to trigger a SELL (tightened to match)

# --- Crypto risk settings (separate from ETF — crypto is more volatile) ---
MAX_CRYPTO_POSITIONS = 2       # Max 2 concurrent crypto positions (limits correlated loss)
MAX_CRYPTO_POSITION_PCT = 0.08 # Max 8% per crypto position (lower — crypto is more volatile)
CRYPTO_RUN_INTERVAL_MINUTES = 30  # Check crypto every 30 minutes

# --- Risk circuit breakers ---
DAILY_LOSS_LIMIT_PCT  = 0.03  # Halt all new trades if daily P&L drops below -3%
BREAKEVEN_ATR_TRIGGER = 1.0   # Move stop to entry when profit >= 1x ATR (lock in breakeven)

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
RUN_INTERVAL_MINUTES = 60     # How often the ETF strategy runs (during market hours)
