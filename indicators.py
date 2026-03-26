import pandas as pd
import numpy as np
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

    Original indicators:
      RSI, MACD, SMA 50/200, Bollinger Bands, Volume ratio, Stochastic

    New indicators (v2):
      EMA 9/21  — faster crossover signal, better for crypto
      ADX       — trend strength filter (avoids choppy/sideways markets)
      OBV       — On-Balance Volume trend (directional volume pressure)
      Supertrend — ATR-based trend-following signal
    """
    if df.empty or len(df) < SMA_LONG + 10:
        logger.warning("Not enough data to calculate all indicators")
        return pd.DataFrame()

    df = df.copy()

    # ------------------------------------------------------------------
    # Original indicators
    # ------------------------------------------------------------------

    # RSI — momentum oscillator (0–100)
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

    # SMA 50/200 — slow trend direction
    df["sma_short"] = ta.trend.SMAIndicator(df["close"], window=SMA_SHORT).sma_indicator()
    df["sma_long"] = ta.trend.SMAIndicator(df["close"], window=SMA_LONG).sma_indicator()

    # EMA 20 — short-term trend
    df["ema_20"] = ta.trend.EMAIndicator(df["close"], window=20).ema_indicator()

    # Bollinger Bands — volatility + mean reversion
    bb = ta.volatility.BollingerBands(df["close"], window=BB_PERIOD, window_dev=BB_STD)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_mid"] = bb.bollinger_mavg()
    df["bb_pct"] = bb.bollinger_pband()

    # ATR — volatility measure (used for stop-loss sizing and Supertrend)
    df["atr"] = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=ATR_PERIOD
    ).average_true_range()

    # Volume ratio vs 20-day average
    df["volume_sma"] = df["volume"].rolling(window=20).mean()
    df["volume_ratio"] = df["volume"] / df["volume_sma"]

    # Stochastic Oscillator — overbought/oversold with direction
    stoch = ta.momentum.StochasticOscillator(
        df["high"], df["low"], df["close"], window=STOCH_PERIOD
    )
    df["stoch_k"] = stoch.stoch()
    df["stoch_d"] = stoch.stoch_signal()

    # ------------------------------------------------------------------
    # NEW: EMA 9 / EMA 21 crossover (faster than SMA 50/200 for crypto)
    # ------------------------------------------------------------------
    df["ema_9"] = ta.trend.EMAIndicator(df["close"], window=9).ema_indicator()
    df["ema_21"] = ta.trend.EMAIndicator(df["close"], window=21).ema_indicator()

    # ------------------------------------------------------------------
    # NEW: ADX — Average Directional Index (trend strength, 0–100)
    # ADX > 25 = trending market (signals are reliable)
    # ADX < 20 = sideways/choppy (signals are noise — filter them out)
    # ------------------------------------------------------------------
    adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], window=14)
    df["adx"] = adx_ind.adx()
    df["adx_pos"] = adx_ind.adx_pos()   # +DI (bullish directional movement)
    df["adx_neg"] = adx_ind.adx_neg()   # -DI (bearish directional movement)

    # ------------------------------------------------------------------
    # NEW: OBV — On-Balance Volume (cumulative volume pressure)
    # Rising OBV = buying pressure. Falling OBV = selling pressure.
    # We use the slope of OBV (vs its 20-period EMA) for the signal.
    # ------------------------------------------------------------------
    df["obv"] = ta.volume.OnBalanceVolumeIndicator(df["close"], df["volume"]).on_balance_volume()
    df["obv_ema"] = df["obv"].ewm(span=20, adjust=False).mean()
    df["obv_trend"] = df["obv"] - df["obv_ema"]  # Positive = bullish OBV momentum

    # ------------------------------------------------------------------
    # NEW: Supertrend (ATR multiplier = 3, period = 10)
    # +1 = price above Supertrend line (bullish), -1 = bearish
    # ------------------------------------------------------------------
    df["supertrend"] = _calculate_supertrend(df, period=10, multiplier=3.0)

    return df.dropna()


def _calculate_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    """
    Supertrend indicator.
    Returns a Series of +1 (bullish) or -1 (bearish) values.
    """
    hl2 = (df["high"] + df["low"]) / 2
    atr = ta.volatility.AverageTrueRange(
        df["high"], df["low"], df["close"], window=period
    ).average_true_range()

    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)

    supertrend = pd.Series(index=df.index, dtype=float)
    direction = pd.Series(index=df.index, dtype=int)

    for i in range(1, len(df)):
        # Upper band
        if upper_band.iloc[i] < upper_band.iloc[i - 1] or df["close"].iloc[i - 1] > upper_band.iloc[i - 1]:
            upper_band.iloc[i] = upper_band.iloc[i]
        else:
            upper_band.iloc[i] = upper_band.iloc[i - 1]

        # Lower band
        if lower_band.iloc[i] > lower_band.iloc[i - 1] or df["close"].iloc[i - 1] < lower_band.iloc[i - 1]:
            lower_band.iloc[i] = lower_band.iloc[i]
        else:
            lower_band.iloc[i] = lower_band.iloc[i - 1]

        # Direction
        if df["close"].iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1] if i > 1 else 1

        supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]

    return direction
