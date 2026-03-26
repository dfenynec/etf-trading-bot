import pandas as pd
import logging

from config import RSI_OVERSOLD, RSI_OVERBOUGHT, MIN_BUY_SCORE, MIN_SELL_SCORE

logger = logging.getLogger(__name__)

# ADX threshold — below this the market is ranging/choppy, signals are unreliable
ADX_TREND_THRESHOLD = 15


def score_etf(df: pd.DataFrame, ticker: str) -> dict:
    """
    Score an asset from -9 to +9 using 10 indicators.

    Scoring breakdown:
      RSI              : -1 / 0 / +1
      MACD             : -2 / -1 / +1 / +2  (crossovers weighted higher)
      SMA 50/200       : -1 / +1             (slow trend — Golden/Death Cross)
      Bollinger Bands  : -1 / 0 / +1
      Volume ratio     : ±1                  (amplifies existing direction)
      Stochastic       : -1 / 0 / +1
      EMA 9/21 cross   : -1 / +1             (NEW — fast crossover)
      OBV trend        : -1 / 0 / +1         (NEW — directional volume pressure)
      Supertrend       : -1 / +1             (NEW — ATR-based trend direction)

    ADX FILTER (NEW):
      If ADX < ADX_TREND_THRESHOLD (20), the market is choppy.
      All scores are halved and the signal is capped at HOLD to avoid
      entering trades in directionless markets.
    """
    if df.empty or len(df) < 3:
        return _empty_signal(ticker)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0
    reasons = []

    # ------------------------------------------------------------------
    # 1. RSI
    # ------------------------------------------------------------------
    if latest["rsi"] < RSI_OVERSOLD:
        score += 1
        reasons.append(f"RSI oversold ({latest['rsi']:.1f})")
    elif latest["rsi"] > RSI_OVERBOUGHT:
        score -= 1
        reasons.append(f"RSI overbought ({latest['rsi']:.1f})")
    else:
        reasons.append(f"RSI neutral ({latest['rsi']:.1f})")

    # ------------------------------------------------------------------
    # 2. MACD (fresh crossovers score ±2, otherwise ±1)
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 3. SMA 50/200 — informational only, not scored
    # (MACD and EMA 9/21 already capture moving average relationships;
    #  scoring SMA 50/200 too would triple-count the same signal)
    # ------------------------------------------------------------------
    import math as _math
    if not _math.isnan(float(latest["sma_long"])):
        sma_trend = "above" if latest["sma_short"] > latest["sma_long"] else "below"
        reasons.append(f"SMA50 {sma_trend} SMA200 (reference only)")
    else:
        reasons.append("SMA200 unavailable (insufficient history)")

    # ------------------------------------------------------------------
    # 4. Bollinger Bands
    # ------------------------------------------------------------------
    if latest["bb_pct"] < 0.15:
        score += 1
        reasons.append("Price near BB lower band (oversold zone)")
    elif latest["bb_pct"] > 0.85:
        score -= 1
        reasons.append("Price near BB upper band (overbought zone)")
    else:
        reasons.append(f"Price in BB mid-zone ({latest['bb_pct']:.2f})")

    # ------------------------------------------------------------------
    # 5. Volume confirmation
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 6. Stochastic
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 7. NEW: EMA 9/21 crossover (faster trend signal for crypto)
    # ------------------------------------------------------------------
    ema_bull = latest["ema_9"] > latest["ema_21"]
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
        reasons.append(f"EMA 9 above EMA 21 (bullish trend)")
    else:
        score -= 1
        reasons.append(f"EMA 9 below EMA 21 (bearish trend)")

    # ------------------------------------------------------------------
    # 8. NEW: OBV trend (directional volume pressure)
    # ------------------------------------------------------------------
    if latest["obv_trend"] > 0 and latest["obv_trend"] > prev["obv_trend"]:
        score += 1
        reasons.append("OBV rising — buying pressure building")
    elif latest["obv_trend"] < 0 and latest["obv_trend"] < prev["obv_trend"]:
        score -= 1
        reasons.append("OBV falling — selling pressure building")
    else:
        reasons.append(f"OBV neutral")

    # ------------------------------------------------------------------
    # 9. NEW: Supertrend direction
    # ------------------------------------------------------------------
    if latest["supertrend"] == 1:
        score += 1
        reasons.append("Supertrend: BULLISH (price above trend line)")
    else:
        score -= 1
        reasons.append("Supertrend: BEARISH (price below trend line)")

    # ------------------------------------------------------------------
    # ADX FILTER: reduce confidence in choppy/ranging markets
    # ------------------------------------------------------------------
    adx_value = latest["adx"]
    trending = adx_value >= ADX_TREND_THRESHOLD

    if not trending:
        # Market is directionless — halve the score and cap at HOLD
        original_score = score
        score = score // 2
        reasons.insert(0, f"⚠ ADX {adx_value:.1f} < {ADX_TREND_THRESHOLD} (choppy market — score halved from {original_score})")

    # ------------------------------------------------------------------
    # Final signal
    # ------------------------------------------------------------------
    if trending and score >= MIN_BUY_SCORE:
        signal = "BUY"
    elif trending and score <= MIN_SELL_SCORE:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "ticker": ticker,
        "score": score,
        "signal": signal,
        "price": round(latest["close"], 4),
        "atr": round(latest["atr"], 6),
        "adx": round(adx_value, 1),
        "trending": trending,
        "reasons": reasons,
    }


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
        "reasons": ["Insufficient data"],
    }
