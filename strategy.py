import math
import pandas as pd
import logging

from config import (
    RSI_OVERSOLD, RSI_OVERBOUGHT, MIN_BUY_SCORE, MIN_SELL_SCORE,
    MR_RSI_OVERSOLD, MR_RSI_OVERBOUGHT, MIN_MR_SCORE,
    ADX_RANGING_THRESHOLD, ADX_TRENDING_THRESHOLD,
)

logger = logging.getLogger(__name__)


def score_etf(df: pd.DataFrame, ticker: str) -> dict:
    """
    Score an asset using regime-adaptive strategy.

    Market regime is determined by ADX first, then the appropriate
    scoring function is applied:

      TRENDING  (ADX >= 25): trend-following indicators
                             (EMA 9/21, MACD, OBV, Supertrend, RSI direction)
                             Score range: -9 to +9, threshold ±4

      RANGING   (ADX < 25):  mean-reversion indicators
                             (Bollinger Bands, RSI extremes, Stochastic)
                             Score range: -5 to +5, threshold ±3

    The transitional zone (ADX 20–25) is treated conservatively as RANGING.
    """
    if df.empty or len(df) < 3:
        return _empty_signal(ticker)

    latest = df.iloc[-1]
    prev   = df.iloc[-2]

    adx_value = latest["adx"]

    # --- Breakout check (overrides regime — detected before ADX classification) ---
    is_breakout, vol_ratio, resistance = _check_breakout(df, latest)
    if is_breakout:
        regime         = "BREAKOUT"
        score, reasons = _score_breakout(latest, prev, vol_ratio, resistance)
        reasons.insert(0, f"Price above 20d high ${resistance:.4f} with {vol_ratio:.1f}x volume — BREAKOUT mode")
        buy_threshold  = 2   # Lower threshold: the breakout itself is strong confirmation
        sell_threshold = 99  # Don't short a breakout
    elif _detect_regime(adx_value) == "TRENDING":
        regime         = "TRENDING"
        score, reasons = _score_trend_following(latest, prev)
        reasons.insert(0, f"ADX {adx_value:.1f} ≥ {ADX_TRENDING_THRESHOLD} — TRENDING → trend-following mode")
        buy_threshold  =  MIN_BUY_SCORE
        sell_threshold =  MIN_SELL_SCORE
    else:
        regime         = "RANGING"
        score, reasons = _score_mean_reversion(latest, prev)
        reasons.insert(0, f"ADX {adx_value:.1f} < {ADX_TRENDING_THRESHOLD} — RANGING → mean-reversion mode")
        buy_threshold  =  MIN_MR_SCORE
        sell_threshold = -MIN_MR_SCORE

    if score >= buy_threshold:
        signal = "BUY"
    elif score <= sell_threshold:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "ticker":     ticker,
        "score":      score,
        "signal":     signal,
        "price":      round(latest["close"], 4),
        "atr":        round(latest["atr"], 6),
        "adx":        round(adx_value, 1),
        "trending":   (regime == "TRENDING"),
        "regime":     regime,
        "resistance": round(resistance, 4) if is_breakout else None,
        "reasons":    reasons,
    }


def _check_breakout(df, latest) -> tuple:
    """
    Detect a price breakout above the 20-day resistance level with volume confirmation.

    Conditions:
      - Current close > highest high of the previous 20 candles
      - Current volume > 1.2x the 20-bar average volume

    Returns:
      (is_breakout: bool, vol_ratio: float, resistance: float)
    """
    if len(df) < 22:
        return False, 1.0, 0.0

    # Resistance = highest high of the 20 candles BEFORE today
    resistance  = float(df["high"].iloc[-21:-1].max())
    current_close = float(latest["close"])

    vol_avg_20  = float(df["volume"].iloc[-21:-1].mean())
    vol_ratio   = float(latest["volume"]) / vol_avg_20 if vol_avg_20 > 0 else 1.0

    is_breakout = (current_close > resistance) and (vol_ratio >= 1.2)
    return is_breakout, vol_ratio, resistance


def _score_breakout(latest, prev, vol_ratio: float, resistance: float) -> tuple:
    """
    Score for breakout regime.

    The price already cleared resistance with volume (+2 base).
    Secondary indicators confirm strength and filter false breakouts.

    Breakdown:
      Breakout confirmed  : +2  (always — it's why we're here)
      Volume spike ≥ 2.0x : +2 / ≥ 1.5x: +1 / else: +0
      MACD bullish        : +1
      RSI not overbought  : +1 / overbought: -1

    Max: +6, entry threshold: +2
    """
    score   = 2   # The breakout itself
    reasons = [f"Breakout above resistance ${resistance:.4f} (+2)"]

    # Volume strength
    if vol_ratio >= 2.0:
        score += 2
        reasons.append(f"Volume surge {vol_ratio:.1f}x avg — strong conviction (+2)")
    elif vol_ratio >= 1.5:
        score += 1
        reasons.append(f"Volume spike {vol_ratio:.1f}x avg (+1)")
    else:
        reasons.append(f"Volume {vol_ratio:.1f}x avg — moderate (+0)")

    # MACD confirmation
    if latest["macd"] > latest["macd_signal"]:
        score += 1
        reasons.append("MACD bullish (+1)")
    else:
        reasons.append("MACD bearish (+0)")

    # RSI filter (overbought breakouts often fail immediately)
    if latest["rsi"] < 70:
        score += 1
        reasons.append(f"RSI not overbought ({latest['rsi']:.1f}) (+1)")
    else:
        score -= 1
        reasons.append(f"RSI overbought ({latest['rsi']:.1f}) — risk of reversal (-1)")

    return score, reasons


def _detect_regime(adx: float) -> str:
    """
    Classify market regime by ADX strength.
      ADX >= ADX_TRENDING_THRESHOLD  → TRENDING
      ADX <  ADX_TRENDING_THRESHOLD  → RANGING  (includes transitional 20–25 zone)
    """
    return "TRENDING" if adx >= ADX_TRENDING_THRESHOLD else "RANGING"


def _score_trend_following(latest, prev) -> tuple:
    """
    Score for clearly trending markets (ADX >= 25).

    Uses momentum and directional indicators that are reliable when
    price is making consistent higher highs or lower lows.

    Breakdown:
      RSI direction    : -1 / 0 / +1
      MACD crossover   : -2 / -1 / +1 / +2  (fresh crossovers weighted higher)
      SMA 50/200       : informational only  (already captured by EMA 9/21)
      Bollinger Bands  : -1 / 0 / +1
      Volume confirm   : ±1                  (amplifies existing direction)
      Stochastic       : -1 / 0 / +1
      EMA 9/21 cross   : -2 / -1 / +1 / +2  (fast crossover)
      OBV trend        : -1 / 0 / +1
      Supertrend       : -1 / +1

    Max range: -9 to +9
    """
    score   = 0
    reasons = []

    # 1. RSI direction
    if latest["rsi"] < RSI_OVERSOLD:
        score += 1
        reasons.append(f"RSI oversold ({latest['rsi']:.1f})")
    elif latest["rsi"] > RSI_OVERBOUGHT:
        score -= 1
        reasons.append(f"RSI overbought ({latest['rsi']:.1f})")
    else:
        reasons.append(f"RSI neutral ({latest['rsi']:.1f})")

    # 2. MACD (fresh crossover = ±2, sustained = ±1)
    macd_bull_cross = latest["macd"] > latest["macd_signal"] and prev["macd"] <= prev["macd_signal"]
    macd_bear_cross = latest["macd"] < latest["macd_signal"] and prev["macd"] >= prev["macd_signal"]
    if macd_bull_cross:
        score += 2
        reasons.append("MACD bullish crossover (strong)")
    elif macd_bear_cross:
        score -= 2
        reasons.append("MACD bearish crossover (strong)")
    elif latest["macd"] > latest["macd_signal"]:
        score += 1
        reasons.append("MACD above signal")
    else:
        score -= 1
        reasons.append("MACD below signal")

    # 3. SMA 50/200 — informational only
    if not math.isnan(float(latest["sma_long"])):
        sma_trend = "above" if latest["sma_short"] > latest["sma_long"] else "below"
        reasons.append(f"SMA50 {sma_trend} SMA200 (reference only)")
    else:
        reasons.append("SMA200 unavailable (insufficient history)")

    # 4. Bollinger Bands
    if latest["bb_pct"] < 0.15:
        score += 1
        reasons.append("Price near BB lower band (oversold zone)")
    elif latest["bb_pct"] > 0.85:
        score -= 1
        reasons.append("Price near BB upper band (overbought zone)")
    else:
        reasons.append(f"Price in BB mid-zone ({latest['bb_pct']:.2f})")

    # 5. Volume confirmation (amplifies existing direction)
    if latest["volume_ratio"] > 1.5:
        if score > 0:
            score += 1
            reasons.append(f"High volume confirms bullish ({latest['volume_ratio']:.1f}x avg)")
        elif score < 0:
            score -= 1
            reasons.append(f"High volume confirms bearish ({latest['volume_ratio']:.1f}x avg)")
        else:
            reasons.append(f"High volume but mixed signals ({latest['volume_ratio']:.1f}x avg)")
    else:
        reasons.append(f"Normal volume ({latest['volume_ratio']:.1f}x avg)")

    # 6. Stochastic
    stoch_bull = latest["stoch_k"] < 20 and latest["stoch_k"] > latest["stoch_d"]
    stoch_bear = latest["stoch_k"] > 80 and latest["stoch_k"] < latest["stoch_d"]
    if stoch_bull:
        score += 1
        reasons.append(f"Stochastic oversold + turning up ({latest['stoch_k']:.1f})")
    elif stoch_bear:
        score -= 1
        reasons.append(f"Stochastic overbought + turning down ({latest['stoch_k']:.1f})")
    else:
        reasons.append(f"Stochastic neutral ({latest['stoch_k']:.1f})")

    # 7. EMA 9/21 crossover (fresh = ±2, sustained = ±1)
    ema_bull       = latest["ema_9"] > latest["ema_21"]
    ema_fresh_bull = ema_bull and prev["ema_9"] <= prev["ema_21"]
    ema_fresh_bear = not ema_bull and prev["ema_9"] >= prev["ema_21"]
    if ema_fresh_bull:
        score += 2
        reasons.append("EMA 9/21 fresh bullish cross (strong)")
    elif ema_fresh_bear:
        score -= 2
        reasons.append("EMA 9/21 fresh bearish cross (strong)")
    elif ema_bull:
        score += 1
        reasons.append("EMA 9 above EMA 21 (bullish trend)")
    else:
        score -= 1
        reasons.append("EMA 9 below EMA 21 (bearish trend)")

    # 8. OBV trend
    if latest["obv_trend"] > 0 and latest["obv_trend"] > prev["obv_trend"]:
        score += 1
        reasons.append("OBV rising — buying pressure building")
    elif latest["obv_trend"] < 0 and latest["obv_trend"] < prev["obv_trend"]:
        score -= 1
        reasons.append("OBV falling — selling pressure building")
    else:
        reasons.append("OBV neutral")

    # 9. Supertrend
    if latest["supertrend"] == 1:
        score += 1
        reasons.append("Supertrend: BULLISH (price above trend line)")
    else:
        score -= 1
        reasons.append("Supertrend: BEARISH (price below trend line)")

    return score, reasons


def _score_mean_reversion(latest, prev) -> tuple:
    """
    Score for ranging/sideways markets (ADX < 25).

    Looks for price stretched too far from its mean and likely to snap back.
    Deliberately excludes trend-following indicators (EMA crossover, MACD,
    OBV trend, Supertrend) which generate false signals in directionless markets.

    Breakdown:
      Bollinger Bands  : -2 / -1 / 0 / +1 / +2  (primary signal)
      RSI extremes     : -2 / -1 / 0 / +1 / +2  (tighter thresholds: 30/70)
      Stochastic       : -1 / 0 / +1             (turning point confirmation)
      Volume caution   : halves score if volume > 2x avg (may be a breakout, not reversion)

    Max range: -5 to +5
    """
    score   = 0
    reasons = []

    # 1. Bollinger Bands — primary mean-reversion signal
    if latest["bb_pct"] < 0.10:
        score += 2
        reasons.append(f"Price at BB lower band — strong oversold ({latest['bb_pct']:.2f})")
    elif latest["bb_pct"] < 0.25:
        score += 1
        reasons.append(f"Price near BB lower band — oversold ({latest['bb_pct']:.2f})")
    elif latest["bb_pct"] > 0.90:
        score -= 2
        reasons.append(f"Price at BB upper band — strong overbought ({latest['bb_pct']:.2f})")
    elif latest["bb_pct"] > 0.75:
        score -= 1
        reasons.append(f"Price near BB upper band — overbought ({latest['bb_pct']:.2f})")
    else:
        reasons.append(f"Price in BB mid-range ({latest['bb_pct']:.2f}) — no edge")

    # 2. RSI extremes (tighter thresholds than trend mode)
    if latest["rsi"] < MR_RSI_OVERSOLD:
        score += 2
        reasons.append(f"RSI deeply oversold ({latest['rsi']:.1f})")
    elif latest["rsi"] < 40:
        score += 1
        reasons.append(f"RSI oversold ({latest['rsi']:.1f})")
    elif latest["rsi"] > MR_RSI_OVERBOUGHT:
        score -= 2
        reasons.append(f"RSI deeply overbought ({latest['rsi']:.1f})")
    elif latest["rsi"] > 60:
        score -= 1
        reasons.append(f"RSI overbought ({latest['rsi']:.1f})")
    else:
        reasons.append(f"RSI neutral ({latest['rsi']:.1f})")

    # 3. Stochastic — turning point confirmation
    stoch_bull = latest["stoch_k"] < 20 and latest["stoch_k"] > latest["stoch_d"]
    stoch_bear = latest["stoch_k"] > 80 and latest["stoch_k"] < latest["stoch_d"]
    if stoch_bull:
        score += 1
        reasons.append(f"Stochastic oversold + turning up ({latest['stoch_k']:.1f})")
    elif stoch_bear:
        score -= 1
        reasons.append(f"Stochastic overbought + turning down ({latest['stoch_k']:.1f})")
    else:
        reasons.append(f"Stochastic neutral ({latest['stoch_k']:.1f})")

    # 4. Volume caution — high volume in a ranging market often signals a breakout,
    #    not a reversion. Halve confidence to avoid fading a genuine move.
    if latest["volume_ratio"] > 2.0:
        score = score // 2
        reasons.append(
            f"Caution: unusual volume in ranging market ({latest['volume_ratio']:.1f}x avg) "
            f"— may be breaking out, confidence halved"
        )
    else:
        reasons.append(f"Normal volume ({latest['volume_ratio']:.1f}x avg) — mean reversion intact")

    return score, reasons


def rank_buy_candidates(signals: list) -> list:
    """Return BUY signals sorted by score descending (best opportunity first)."""
    return sorted(
        [s for s in signals if s["signal"] == "BUY"],
        key=lambda x: x["score"],
        reverse=True,
    )


def rank_sell_candidates(signals: list) -> list:
    """Return SELL signals sorted by score ascending (most negative = strongest short first)."""
    return sorted(
        [s for s in signals if s["signal"] == "SELL"],
        key=lambda x: x["score"],
    )


def _empty_signal(ticker: str) -> dict:
    return {
        "ticker": ticker, "score": 0, "signal": "HOLD",
        "price": 0, "atr": 0, "adx": 0, "trending": False,
        "regime": "RANGING", "reasons": ["Insufficient data"],
    }
