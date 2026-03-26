"""
Trade Journal — logs every trade to a CSV file for performance analysis.
Records: timestamp, action, symbol, qty, price, score, stop, take_profit, note.
"""
import csv
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

JOURNAL_FILE = "trade_journal.csv"
_COLUMNS = ["timestamp", "action", "symbol", "qty", "price", "score", "stop", "take_profit", "note"]


def _ensure_header() -> None:
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writeheader()


def log_trade(
    action: str,
    symbol: str,
    qty: float,
    price: float,
    score: int = 0,
    stop: float = None,
    take_profit: float = None,
    note: str = "",
) -> None:
    """
    Append one trade row to trade_journal.csv.

    Args:
        action:      "BUY" | "SELL" | "SHORT" | "COVER"
        symbol:      Ticker or crypto pair
        qty:         Number of shares / units
        price:       Execution price
        score:       Indicator score at trade time
        stop:        Stop-loss price (bracket order)
        take_profit: Take-profit price (bracket order)
        note:        Free-text note (e.g. "Stop-loss hit", "Signal exit")
    """
    _ensure_header()
    row = {
        "timestamp":   datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action":      action,
        "symbol":      symbol,
        "qty":         round(float(qty), 6),
        "price":       round(float(price), 6),
        "score":       score,
        "stop":        round(float(stop), 6) if stop else "",
        "take_profit": round(float(take_profit), 6) if take_profit else "",
        "note":        note,
    }
    try:
        with open(JOURNAL_FILE, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writerow(row)
        logger.info(f"[JOURNAL] {action:<6} {symbol:<10} qty={qty:.4f} @ ${price:.4f}  score={score}  {note}")
    except Exception as e:
        logger.error(f"[JOURNAL] Failed to write trade: {e}")
