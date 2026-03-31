import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import logging

from config import LOOKBACK_DAYS

logger = logging.getLogger(__name__)


def _clean_df(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten multi-level columns, lowercase, sort by date."""
    if df.empty:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    return df


# ---------------------------------------------------------------------------
# Daily data (macro trend filter — is the asset in a long-term uptrend?)
# ---------------------------------------------------------------------------

def fetch_etf_data(ticker: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Fetch daily OHLCV for a single ETF (used as macro trend filter)."""
    end   = datetime.now()
    start = end - timedelta(days=days)
    try:
        df = yf.download(ticker, start=start, end=end,
                         interval="1d", progress=False, auto_adjust=True)
        df = _clean_df(df)
        if df.empty:
            logger.warning(f"No daily data for {ticker}")
        return df
    except Exception as e:
        logger.error(f"Error fetching daily data for {ticker}: {e}")
        return pd.DataFrame()


def fetch_all_etfs(tickers: list) -> dict:
    """Fetch daily data for all ETFs. Returns {ticker: DataFrame}."""
    return {t: df for t in tickers
            if not (df := fetch_etf_data(t)).empty}


def alpaca_to_yfinance(symbol: str) -> str:
    return symbol.replace("/", "-")


def fetch_crypto_data(symbol: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Fetch daily OHLCV for a crypto pair (macro trend filter)."""
    return fetch_etf_data(alpaca_to_yfinance(symbol), days=days)


def fetch_all_crypto(symbols: list) -> dict:
    """Fetch daily data for all crypto symbols. Returns {alpaca_symbol: DataFrame}."""
    data = {}
    for symbol in symbols:
        df = fetch_crypto_data(symbol)
        if not df.empty:
            data[symbol] = df
    return data


# ---------------------------------------------------------------------------
# 15-minute data (primary signal source — responsive, high trade frequency)
# ---------------------------------------------------------------------------

def fetch_etf_data_15min(ticker: str) -> pd.DataFrame:
    """
    Fetch 15-minute OHLCV bars for a single ETF.
    period='60d' → ~1,560 bars (60 days × 6.5 market hours × 4 bars/hour).
    More than enough for SMA200 and all indicators.
    """
    try:
        df = yf.download(ticker, period="60d", interval="15m",
                         progress=False, auto_adjust=True)
        df = _clean_df(df)
        if df.empty:
            logger.warning(f"No 15m data for {ticker}")
        else:
            logger.debug(f"Fetched {len(df)} 15m bars for {ticker}")
        return df
    except Exception as e:
        logger.error(f"Error fetching 15m data for {ticker}: {e}")
        return pd.DataFrame()


def fetch_all_etfs_hourly(tickers: list) -> dict:
    """Fetch 15-minute data for all ETFs. Returns {ticker: DataFrame}."""
    return {t: df for t in tickers
            if not (df := fetch_etf_data_15min(t)).empty}


def fetch_crypto_data_hourly(symbol: str) -> pd.DataFrame:
    """
    Fetch 15-minute OHLCV bars for a crypto pair.
    period='60d' → ~5,760 bars (60 days × 24h × 4 bars/hour).
    """
    yf_symbol = alpaca_to_yfinance(symbol)
    try:
        df = yf.download(yf_symbol, period="60d", interval="15m",
                         progress=False, auto_adjust=True)
        df = _clean_df(df)
        if df.empty:
            logger.warning(f"No 15m data for {symbol}")
        return df
    except Exception as e:
        logger.error(f"Error fetching 15m data for {symbol}: {e}")
        return pd.DataFrame()


def fetch_all_crypto_hourly(symbols: list) -> dict:
    """Fetch 15-minute data for all crypto symbols. Returns {alpaca_symbol: DataFrame}."""
    data = {}
    for symbol in symbols:
        df = fetch_crypto_data_hourly(symbol)
        if not df.empty:
            data[symbol] = df
    return data
