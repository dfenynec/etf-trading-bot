"""
Risk management — position sizing using the 1% risk rule.

Instead of allocating a fixed % of portfolio per trade, we size
every position so the maximum loss (entry → stop-loss) equals
exactly 1% of portfolio value.

Example on a $95,000 portfolio:
  risk_dollars    = $95,000 × 1%  = $950
  stop distance   = $87.90 - $78.77 = $9.13 per unit
  qty             = $950 / $9.13   = 104 units

This means:
  - Wide stops  → smaller position (fewer shares / units)
  - Tight stops → larger position  (more shares / units)
  - Dollar risk is always capped at 1% regardless of volatility

A hard cap (MAX_POSITION_PCT / MAX_CRYPTO_POSITION_PCT) prevents
over-sizing when stop distances are very tight.
"""
import math
import logging

from config import (
    MAX_POSITION_PCT, MAX_CRYPTO_POSITION_PCT,
    STOP_LOSS_ATR_MULT, TAKE_PROFIT_ATR_MULT,
    STOP_LOSS_MAX_PCT, TAKE_PROFIT_MAX_PCT,
    RISK_PER_TRADE_PCT,
)

logger = logging.getLogger(__name__)


def calculate_position_size(
    portfolio_value: float,
    price: float,
    stop_price: float,
    score: int = 0,
    atr: float = 0.0,
) -> int:
    """
    Size an ETF position using the 1% risk rule.

    Args:
        portfolio_value: Current total portfolio equity
        price:           Current asset price (entry)
        stop_price:      Stop-loss price (must be below entry for longs)
        score:           Indicator score (unused now — kept for API compatibility)
        atr:             ATR (unused now — kept for API compatibility)

    Returns:
        Integer number of shares, minimum 1.
    """
    risk_dollars  = portfolio_value * RISK_PER_TRADE_PCT
    stop_distance = abs(price - stop_price)

    if stop_distance <= 0:
        # Fallback: use base 5% allocation if stop is miscalculated
        shares = int(portfolio_value * 0.05 / price)
        return max(1, shares)

    qty_by_risk = risk_dollars / stop_distance

    # Hard cap: never commit more than MAX_POSITION_PCT of portfolio
    qty_by_cap = int(portfolio_value * MAX_POSITION_PCT / price)

    shares = int(min(qty_by_risk, qty_by_cap))

    logger.debug(
        f"ETF sizing: risk=${risk_dollars:.0f} stop_dist=${stop_distance:.4f} "
        f"→ {shares} shares @ ${price:.4f} (cap={qty_by_cap})"
    )
    return max(1, shares)


def calculate_crypto_position_size(
    portfolio_value: float,
    price: float,
    stop_price: float,
    buying_power: float,
    risk_pct: float = None,
) -> float:
    """
    Size a crypto position using the 1% risk rule with fractional units.

    Args:
        portfolio_value: Current total portfolio equity
        price:           Current crypto price
        stop_price:      Stop-loss price
        buying_power:    Available cash (hard ceiling, with safety buffer applied by caller)
        risk_pct:        Override risk fraction (Kelly-adjusted). Defaults to RISK_PER_TRADE_PCT.

    Returns:
        Float quantity (up to 6 decimal places), floored to avoid overspend.
    """
    effective_risk = risk_pct if risk_pct is not None else RISK_PER_TRADE_PCT
    risk_dollars  = portfolio_value * effective_risk
    stop_distance = abs(price - stop_price)

    if stop_distance <= 0:
        fallback_dollars = min(portfolio_value * MAX_CRYPTO_POSITION_PCT, buying_power)
        return math.floor(fallback_dollars / price * 1_000_000) / 1_000_000

    qty_by_risk = risk_dollars / stop_distance

    # Hard cap: never commit more than MAX_CRYPTO_POSITION_PCT of portfolio or buying power
    qty_by_pct = (portfolio_value * MAX_CRYPTO_POSITION_PCT) / price
    qty_by_bp  = buying_power / price

    qty = min(qty_by_risk, qty_by_pct, qty_by_bp)

    logger.debug(
        f"Crypto sizing: risk=${risk_dollars:.0f} stop_dist=${stop_distance:.6f} "
        f"→ {qty:.6f} units @ ${price:.4f}"
    )
    return math.floor(qty * 1_000_000) / 1_000_000


def calculate_stop_loss(entry_price: float, atr: float) -> float:
    """
    Stop-loss = entry - (STOP_LOSS_ATR_MULT × ATR),
    but never more than STOP_LOSS_MAX_PCT below entry.
    This prevents ATR from creating unreachably wide stops in volatile markets.
    """
    atr_stop  = entry_price - (STOP_LOSS_ATR_MULT * atr)
    pct_floor = entry_price * (1 - STOP_LOSS_MAX_PCT)
    return round(max(atr_stop, pct_floor), 4)


def calculate_take_profit(entry_price: float, atr: float) -> float:
    """
    Take-profit = entry + (TAKE_PROFIT_ATR_MULT × ATR),
    but never more than TAKE_PROFIT_MAX_PCT above entry.
    This keeps targets realistic even when ATR is unusually large.
    """
    atr_tp    = entry_price + (TAKE_PROFIT_ATR_MULT * atr)
    pct_ceil  = entry_price * (1 + TAKE_PROFIT_MAX_PCT)
    return round(min(atr_tp, pct_ceil), 4)
