"""
Dynamic universe screener.

Instead of trading a fixed hardcoded list, the screener ranks a wide pool of
candidates by momentum and returns the top N for the strategy to trade.

Scoring formula (per asset):
  60%  20-day Rate of Change (ROC)    — primary momentum signal
  20%  Volume trend: 5d vs 20d avg   — rising volume = growing interest
  20%  Price vs SMA 50               — only reward assets in an uptrend

ETF flow:   downloads 60d OHLCV for ~35 candidates → liquidity filter → top N
Crypto flow: re-uses already-fetched base_data → ranks in-memory → top N
"""
import logging

import pandas as pd
import yfinance as yf

from config import SCREEN_TOP_N_ETF, SCREEN_TOP_N_CRYPTO, SCREEN_MIN_VOLUME

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Candidate universes  (the wider pools the screener filters from)
# ---------------------------------------------------------------------------

ETF_CANDIDATES = [
    # Broad market
    "SPY", "QQQ", "IWM", "VTI", "DIA", "MDY", "IJR",
    # Technology
    "XLK", "SOXX", "IGV",
    # Healthcare
    "XLV", "IBB", "XBI",
    # Financials
    "XLF", "KRE",
    # Energy
    "XLE", "XOP",
    # Consumer Discretionary / Staples
    "XLY", "XLP",
    # Materials
    "XLB",
    # Real Estate
    "VNQ",
    # Utilities
    "XLU",
    # Commodities — gold, silver, oil, gas
    "GLD", "SLV", "USO", "UNG",
    # Bonds
    "TLT", "IEF", "HYG",
    # International
    "EFA", "EEM",
    # Industrials
    "XLI",
    # Communication
    "XLC",
    # Dividend
    "SCHD", "VYM",
    # Growth / Value rotation
    "VUG", "VTV",
]

# BTC/USD is intentionally excluded from active trading — it's used only as
# a correlation filter in live_trader.py. Its lower % volatility and high
# market efficiency make it a worse candidate than mid-cap alts for a small
# account. We watch it via the BTC filter instead.
CRYPTO_CANDIDATES = [
    # Mid/large cap — high % volatility, good for momentum strategies
    "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "AVAX/USD",
    # Layer-1 smart contract platforms
    "ADA/USD", "ALGO/USD", "NEAR/USD", "HBAR/USD",
    # DeFi / infrastructure
    "LINK/USD", "AAVE/USD", "ATOM/USD",
    # Payments / store of value
    "LTC/USD", "BCH/USD",
    # High momentum / newer
    "WIF/USD", "SHIB/USD",
    # BTC kept for correlation filter — fetched separately in live_trader.py
    "BTC/USD",
]


# ---------------------------------------------------------------------------
# Momentum scoring
# ---------------------------------------------------------------------------

def _momentum_score(df: pd.DataFrame) -> float:
    """
    Composite momentum score for one asset.
    Requires at least 25 rows of daily OHLCV data.
    Columns must be lowercase: open, high, low, close, volume.
    """
    if df is None or len(df) < 25:
        return -999.0

    close  = df["close"]
    volume = df["volume"]

    # 1. 20-day Rate of Change
    roc_20 = (close.iloc[-1] - close.iloc[-21]) / close.iloc[-21] * 100

    # 2. Volume trend: recent 5-day avg vs 20-day avg (as a % change)
    vol_recent  = volume.iloc[-5:].mean()
    vol_avg     = volume.iloc[-20:].mean()
    vol_trend   = (vol_recent / vol_avg - 1) * 100 if vol_avg > 0 else 0.0

    # 3. Trend direction: +5 if above SMA50, -5 if below
    sma50  = close.rolling(50).mean().iloc[-1]
    trend  = 5.0 if close.iloc[-1] > sma50 else -5.0

    return roc_20 * 0.60 + vol_trend * 0.20 + trend * 0.20


# ---------------------------------------------------------------------------
# ETF screener
# ---------------------------------------------------------------------------

def screen_etfs(top_n: int = None) -> list:
    """
    Download 60 days of OHLCV for all ETF candidates, apply a liquidity filter,
    rank by momentum score, and return the top N ticker symbols.

    Falls back to the first top_n candidates if the download fails entirely.
    """
    top_n = top_n or SCREEN_TOP_N_ETF
    logger.info(f"[SCREENER] Scanning {len(ETF_CANDIDATES)} ETF candidates → picking top {top_n} ...")

    try:
        raw = yf.download(
            ETF_CANDIDATES,
            period="60d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as e:
        logger.error(f"[SCREENER] ETF download failed: {e} — falling back to first {top_n}")
        return ETF_CANDIDATES[:top_n]

    scores = {}
    for ticker in ETF_CANDIDATES:
        try:
            # yfinance multi-ticker → MultiIndex columns: (metric, ticker)
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw.xs(ticker, axis=1, level=1).dropna()
            else:
                df = raw.dropna()

            df.columns = [c.lower() for c in df.columns]

            if len(df) < 25:
                logger.debug(f"[SCREENER] {ticker} skipped: only {len(df)} rows")
                continue

            # Liquidity filter — skip thinly traded ETFs
            avg_vol = df["volume"].iloc[-20:].mean()
            if avg_vol < SCREEN_MIN_VOLUME:
                logger.debug(f"[SCREENER] {ticker} skipped: avg vol {avg_vol:,.0f} < {SCREEN_MIN_VOLUME:,}")
                continue

            scores[ticker] = _momentum_score(df)

        except Exception as e:
            logger.debug(f"[SCREENER] {ticker} error: {e}")
            continue

    if not scores:
        logger.warning("[SCREENER] No ETFs passed filters — using fallback list")
        return ETF_CANDIDATES[:top_n]

    ranked = sorted(scores, key=scores.get, reverse=True)[:top_n]

    logger.info(f"[SCREENER] Top {top_n} ETFs by momentum score:")
    for t in ranked:
        logger.info(f"  {t:<6}  {scores[t]:+.2f}")

    return ranked


# ---------------------------------------------------------------------------
# Crypto screener
# ---------------------------------------------------------------------------

def screen_crypto(base_data: dict, top_n: int = None) -> list:
    """
    Rank crypto candidates by momentum using the already-fetched base_data dict.
    Re-uses existing data — no extra API calls needed.
    Returns the top N symbols in Alpaca format ('BTC/USD').
    """
    top_n = top_n or SCREEN_TOP_N_CRYPTO

    scores = {}
    for symbol in CRYPTO_CANDIDATES:
        df = base_data.get(symbol)
        if df is None or df.empty:
            continue
        scores[symbol] = _momentum_score(df)

    if not scores:
        logger.warning("[SCREENER] No crypto data available — using first N candidates")
        return CRYPTO_CANDIDATES[:top_n]

    ranked = sorted(scores, key=scores.get, reverse=True)[:top_n]

    logger.info(f"[SCREENER] Top {top_n} crypto by momentum score:")
    for s in ranked:
        logger.info(f"  {s:<10}  {scores[s]:+.2f}")

    return ranked
