import pandas as pd
import logging

from config import RSI_OVERSOLD, RSI_OVERBOUGHT, MIN_BUY_SCORE, MIN_SELL_SCORE

logger = logging.getLogger(__name__)


def score_etf(df: pd.DataFrame, ticker: str) -> dict:
    """
    Score an ETF from -7 to +7 using 6 indicators.

    Score guide:
      >= MIN_BUY_SCORE  → BUY
      <= MIN_SELL_SCORE → SELL
      in between        → HOLD

    Each indicator contributes:
      RSI          : -1 / 0 / +1
      MACD         : -2 / -1 / 0 / +1 / +2  (crossovers weighted higher)
      SMA trend    : -1 / +1
      Bollinger    : -1 / 0 / +1
      Volume       : amplifies signal by ±1 if high volume
      Stochastic   : -1 / 0 / +1
    """
    if df.empty or len(df) < 3:
        return _empty_signal(ticker)

    latest = df.iloc[-1]
    prev = df.iloc[-2]
    score = 0
    reasons = []

    # 1. RSI
    if latest["rsi"] < RSI_OVERSOLD:
        score += 1
        reasons.append(f"RSI oversold ({latest['rsi']:.1f})")
    elif latest["rsi"] > RSI_OVERBOUGHT:
        score -= 1
        reasons.append(f"RSI overbought ({latest['rsi']:.1f})")
    else:
        reasons.append(f"RSI neutral ({latest['rsi']:.1f})")

    # 2. MACD (fresh crossovers score ±2, otherwise ±1)
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

    # 3. SMA 50/200 trend (Golden Cross / Death Cross)
    if latest["sma_short"] > latest["sma_long"]:
        score += 1
        reasons.append("Golden Cross (SMA50 > SMA200)")
    else:
        score -= 1
        reasons.append("Death Cross (SMA50 < SMA200)")

    # 4. Bollinger Bands position
    if latest["bb_pct"] < 0.15:
        score += 1
        reasons.append(f"Price near BB lower band (oversold zone)")
    elif latest["bb_pct"] > 0.85:
        score -= 1
        reasons.append(f"Price near BB upper band (overbought zone)")
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

    # 6. Stochastic — oversold/overbought with direction
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

    # Determine signal
    if score >= MIN_BUY_SCORE:
        signal = "BUY"
    elif score <= MIN_SELL_SCORE:
        signal = "SELL"
    else:
        signal = "HOLD"

    return {
        "ticker": ticker,
        "score": score,
        "signal": signal,
        "price": round(latest["close"], 2),
        "atr": round(latest["atr"], 4),
        "reasons": reasons,
    }


def rank_buy_candidates(signals: list) -> list:
    """Return BUY signals sorted by score descending (best opportunity first)."""
    return sorted(
        [s for s in signals if s["signal"] == "BUY"],
        key=lambda x: x["score"],
        reverse=True,
    )


def _empty_signal(ticker: str) -> dict:
    return {"ticker": ticker, "score": 0, "signal": "HOLD", "price": 0, "atr": 0, "reasons": ["Insufficient data"]}
