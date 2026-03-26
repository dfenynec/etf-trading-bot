import logging

from config import MAX_POSITIONS, MAX_POSITION_PCT, STOP_LOSS_ATR_MULT, TAKE_PROFIT_ATR_MULT, MIN_BUY_SCORE

logger = logging.getLogger(__name__)

# Minimum allocation per trade (% of portfolio)
_BASE_PCT = 0.10

# How much extra allocation per score point above the minimum threshold
_SCORE_STEP = 0.02


def calculate_position_size(
    portfolio_value: float,
    price: float,
    score: int = 0,
    atr: float = 0.0,
) -> int:
    """
    Dynamic position sizing based on signal strength and volatility.

    Allocation scales with score:
      MIN_BUY_SCORE (2) → 10%  (base)
      score 3           → 12%
      score 4           → 14%
      score 5           → 16%
      score 6           → 18%
      score 7+          → 20%  (MAX_POSITION_PCT cap)

    Volatility adjustment:
      If ATR/price > 3%, scale the allocation down proportionally.
      High volatility = smaller position = same dollar risk per trade.
    """
    # Score-based allocation (capped at MAX_POSITION_PCT)
    score_bonus = max(0, abs(score) - MIN_BUY_SCORE) * _SCORE_STEP
    allocation_pct = min(_BASE_PCT + score_bonus, MAX_POSITION_PCT)

    # Volatility adjustment: scale down if ATR is large relative to price
    if atr > 0 and price > 0:
        atr_pct = atr / price
        if atr_pct > 0.03:                        # ATR > 3% of price
            allocation_pct *= 0.03 / atr_pct      # Inverse-scale to keep risk constant

    max_dollars = portfolio_value * allocation_pct
    shares = int(max_dollars / price)

    logger.debug(
        f"Position size: score={score} atr_pct={atr/price*100:.1f}% "
        f"allocation={allocation_pct*100:.1f}% → {shares} shares @ ${price:.4f}"
    )
    return max(1, shares)


def calculate_stop_loss(entry_price: float, atr: float) -> float:
    """Stop-loss placed STOP_LOSS_ATR_MULT * ATR below entry."""
    return round(entry_price - (STOP_LOSS_ATR_MULT * atr), 2)


def calculate_take_profit(entry_price: float, atr: float) -> float:
    """Take-profit placed TAKE_PROFIT_ATR_MULT * ATR above entry."""
    return round(entry_price + (TAKE_PROFIT_ATR_MULT * atr), 2)


def can_open_position(current_count: int, ticker: str, held_tickers: list) -> tuple[bool, str]:
    """
    Returns (True, 'OK') if a new position can be opened,
    or (False, reason) if it cannot.
    """
    if ticker in held_tickers:
        return False, f"Already holding {ticker}"
    if current_count >= MAX_POSITIONS:
        return False, f"Max {MAX_POSITIONS} positions already open"
    return True, "OK"


def check_exit_conditions(position, current_price: float, stop_loss: float, take_profit: float) -> str:
    """
    Returns 'SELL' if stop-loss or take-profit is triggered, else 'HOLD'.
    Note: stop_loss and take_profit must be stored externally (e.g., in a file or DB).
    This function is called optionally for manual SL/TP enforcement.
    """
    if current_price <= stop_loss:
        return "SELL"
    if current_price >= take_profit:
        return "SELL"
    return "HOLD"
