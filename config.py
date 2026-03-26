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
SCREEN_TOP_N_CRYPTO = 5           # Trade the top 5 crypto pairs by momentum (from 9 candidates)
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
TAKE_PROFIT_ATR_MULT = 4.0    # Take-profit = entry + 4 * ATR (2:1 reward/risk)
MIN_BUY_SCORE = 3             # Minimum score to trigger a BUY (raised from 2)
MIN_SELL_SCORE = -3           # Maximum score to trigger a SELL (tightened from -2)

# --- Crypto risk settings (separate from ETF — crypto is more volatile) ---
MAX_CRYPTO_POSITIONS = 3       # Max concurrent crypto positions (reduced from 5)
MAX_CRYPTO_POSITION_PCT = 0.08 # Max 8% per crypto position (lower — crypto is more volatile)
CRYPTO_RUN_INTERVAL_MINUTES = 30  # Check crypto every 30 minutes

# --- Risk circuit breakers ---
DAILY_LOSS_LIMIT_PCT  = 0.03  # Halt all new trades if daily P&L drops below -3%
BREAKEVEN_ATR_TRIGGER = 1.0   # Move stop to entry when profit >= 1x ATR (lock in breakeven)

# --- Correlation filter ---
BTC_CORRELATION_FILTER = True # Suppress altcoin BUYs when BTC score is negative

# --- Performance ---
POSITION_CACHE_TTL = 30       # Seconds to cache get_positions() (reduces API calls)

# --- Bot settings ---
LOOKBACK_DAYS = 300           # Days of history to fetch for indicators
RUN_INTERVAL_MINUTES = 60     # How often the ETF strategy runs (during market hours)
