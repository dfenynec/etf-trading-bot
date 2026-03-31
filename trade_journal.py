"""
Trade Journal — logs every trade to PostgreSQL (primary) and CSV (backup).
Records: timestamp, action, symbol, qty, price, score, stop, take_profit, note, pnl_pct.
"""
import csv
import logging
import os
import re
from datetime import datetime

import db

logger = logging.getLogger(__name__)

JOURNAL_FILE = "trade_journal.csv"
_COLUMNS = ["timestamp", "action", "symbol", "qty", "price", "score", "stop", "take_profit", "note"]


def _ensure_header() -> None:
    if not os.path.exists(JOURNAL_FILE):
        with open(JOURNAL_FILE, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=_COLUMNS).writeheader()


def _parse_pnl(note: str) -> float | None:
    match = re.search(r"pnl=([+-]?\d+\.?\d*)%", note)
    return float(match.group(1)) if match else None


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
    pnl_pct = _parse_pnl(note) if action == "CLOSE" else None

    # --- Primary: PostgreSQL ---
    db.insert_trade(action, symbol, float(qty), float(price),
                    score, stop, take_profit, note, pnl_pct)

    # --- Backup: CSV ---
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
    except Exception as e:
        logger.error(f"[JOURNAL] CSV write failed: {e}")

    logger.info(f"[JOURNAL] {action:<6} {symbol:<10} qty={qty:.4f} @ ${price:.4f}  score={score}  {note}")
