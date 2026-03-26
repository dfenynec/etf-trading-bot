import logging

from config import MAX_POSITIONS, MAX_POSITION_PCT, STOP_LOSS_ATR_MULT, TAKE_PROFIT_ATR_MULT

logger = logging.getLogger(__name__)


def calculate_position_size(portfolio_value: float, price: float) -> int:
    """
    Calculate how many shares to buy.
    Limits each position to MAX_POSITION_PCT of total portfolio value.
    """
    max_dollars = portfolio_value * MAX_POSITION_PCT
    shares = int(max_dollars / price)
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
