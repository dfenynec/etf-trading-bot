import yfinance as yf
import pandas as pd
from datetime import datetime, timedelta
import logging

from config import LOOKBACK_DAYS

logger = logging.getLogger(__name__)


def fetch_etf_data(ticker: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Fetch historical OHLCV data for a single ETF via Yahoo Finance."""
    end = datetime.now()
    start = end - timedelta(days=days)

    try:
        df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
        if df.empty:
            logger.warning(f"No data returned for {ticker}")
            return pd.DataFrame()

        # Flatten multi-level columns if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index)
        df = df.sort_index()

        logger.debug(f"Fetched {len(df)} rows for {ticker}")
        return df

    except Exception as e:
        logger.error(f"Error fetching data for {ticker}: {e}")
        return pd.DataFrame()


def fetch_all_etfs(tickers: list) -> dict:
    """Fetch data for all ETFs in the universe. Returns {ticker: DataFrame}."""
    data = {}
    for ticker in tickers:
        df = fetch_etf_data(ticker)
        if not df.empty:
            data[ticker] = df
    return data


def alpaca_to_yfinance(symbol: str) -> str:
    """Convert Alpaca crypto symbol (BTC/USD) to yfinance format (BTC-USD)."""
    return symbol.replace("/", "-")


def fetch_crypto_data(symbol: str, days: int = LOOKBACK_DAYS) -> pd.DataFrame:
    """Fetch historical OHLCV data for a crypto pair via Yahoo Finance."""
    yf_symbol = alpaca_to_yfinance(symbol)
    return fetch_etf_data(yf_symbol, days=days)


def fetch_all_crypto(symbols: list) -> dict:
    """Fetch data for all crypto symbols. Returns {alpaca_symbol: DataFrame}."""
    data = {}
    for symbol in symbols:
        df = fetch_crypto_data(symbol)
        if not df.empty:
            data[symbol] = df
    return data
