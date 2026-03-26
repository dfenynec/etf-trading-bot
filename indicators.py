import pandas as pd
import ta
import logging

from config import (
    RSI_PERIOD, MACD_FAST, MACD_SLOW, MACD_SIGNAL,
    SMA_SHORT, SMA_LONG, BB_PERIOD, BB_STD, ATR_PERIOD, STOCH_PERIOD
)

logger = logging.getLogger(__name__)


def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all technical indicators to the DataFrame.
    Returns a new DataFrame with indicator columns appended.
    """
    if df.empty or len(df) < SMA_LONG + 10:
        logger.warning("Not enough data to calculate all indicators")
        return pd.DataFrame()

    df = df.copy()

    # RSI — momentum oscillator (0-100)
    df["rsi"] = ta.momentum.RSIIndicator(df["close"], window=RSI_PERIOD).rsi()

    # MACD — trend-following momentum
    macd = ta.trend.MACD(
        df["close"],
        window_fast=MACD_FAST,
        window_slow=MACD_SLOW,
        window_sign=MACD_SIGNAL,
    )
    df["macd"] = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"] = macd.macd_diff()

    # Simple Moving Averages — trend direction
    df["sma_short"] = ta.trend.SMAIndicator(df["close"], window=SMA_SHORT).sma_indicator()
    df["sma_long"] = ta.trend.SMAIndicator(df["close"], window=SMA_LONG).sma_indicator()

    # EMA 20 — short-term trend
    df["ema_20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()

    # Bollinger Bands — volatility + mean reversion
    bb = ta.volatility.BollingerBands(df["close"], window=BB_PERIOD, window_dev=BB_STD)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_pct"] = bb.bollinger_pband()   # 0 = at lower band, 1 = at upper band

    # ATR — average true range (volatility measure, used for stop-loss sizing)
    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=ATR_PERIOD
    ).average_true_range()

    # Volume ratio vs 20-day average
    df["volume_sma"] = df["volume"].rolling(window=20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_sma"]

    # Stochastic Oscillator — overbought/oversold with momentum direction
    stoch = ta.momentum.StochasticOscillator(
        df["high"], df["low"], df["close"], window=STOCH_PERIOD
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    return df.dropna()
