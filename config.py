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
MAX_POSITION_PCT = 0.15       # Max 15% of portfolio per position
STOP_LOSS_ATR_MULT = 2.0      # Stop-loss = entry - 2 * ATR
TAKE_PROFIT_ATR_MULT = 3.0    # Take-profit = entry + 3 * ATR
MIN_BUY_SCORE = 3             # Minimum score to trigger a BUY
MIN_SELL_SCORE = -3           # Maximum score to trigger a SELL

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
MAX_CRYPTO_POSITIONS = 3       # Fewer positions, higher volatility
MAX_CRYPTO_POSITION_PCT = 0.10 # Max 10% of portfolio per crypto position
CRYPTO_RUN_INTERVAL_MINUTES = 30  # Check crypto every 30 minutes

# --- Bot settings ---
LOOKBACK_DAYS = 300           # Days of history to fetch for indicators
RUN_INTERVAL_MINUTES = 60     # How often the ETF strategy runs (during market hours)
