import os
from dotenv import load_dotenv

load_dotenv()

# --- Alpaca credentials (loaded from .env) ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER_TRADING = True  # Switch to False ONLY when ready for real money

# --- ETF Universe (bot picks from these) ---
ETF_UNIVERSE = [
    "SPY",   # S&P 500
    "QQQ",   # Nasdaq 100
    "VTI",   # Total US Market
    "IWM",   # Russell 2000 (small-cap)
    "GLD",   # Gold
    "TLT",   # Long-term US Bonds
    "XLE",   # Energy sector
    "XLF",   # Financials sector
    "XLK",   # Technology sector
    "XLV",   # Healthcare sector
    "SCHD",  # Dividend ETF
    "VNQ",   # Real Estate
]

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

# --- Crypto Universe (trades 24/7) ---
CRYPTO_UNIVERSE = [
    "BTC/USD",   # Bitcoin
    "ETH/USD",   # Ethereum
    "SOL/USD",   # Solana
    "AVAX/USD",  # Avalanche
    "LINK/USD",  # Chainlink
    "DOT/USD",   # Polkadot
    "AAVE/USD",  # Aave
]

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
