"""
Performance dashboard — calculates trading metrics from trade_journal.csv.

Metrics tracked:
  Win rate      : % of closed trades that were profitable
  Avg win/loss  : average P&L % on winning vs losing trades
  Profit factor : sum(wins) / abs(sum(losses))  — target > 1.5
  Total P&L     : sum of all closed trade P&Ls

Called from bot.py at startup and printed to Railway logs.
Can also be run standalone: python performance.py
"""
import csv
import logging
import os
import re

logger = logging.getLogger(__name__)
JOURNAL_FILE = "trade_journal.csv"


def _parse_pnl(note: str) -> float | None:
    """Extract P&L % from note strings like 'TP hit | entry=$87.91 | pnl=+15.58%'"""
    match = re.search(r"pnl=([+-]?\d+\.?\d*)%", note)
    return float(match.group(1)) if match else None


def get_stats() -> dict:
    """
    Read trade_journal.csv and return a performance summary dict.

    Only CLOSE rows have reliable P&L data (bracket order exits).
    SELL/COVER signal exits are counted as trades but P&L is not parsed
    since entry price isn't stored in those rows.
    """
    if not os.path.exists(JOURNAL_FILE):
        return {"error": "trade_journal.csv not found — no trades logged yet"}

    total_entries = 0
    pnls = []

    try:
        with open(JOURNAL_FILE, newline="") as f:
            for row in csv.DictReader(f):
                action = row.get("action", "")
                if action in ("BUY", "SHORT"):
                    total_entries += 1
                elif action == "CLOSE":
                    pnl = _parse_pnl(row.get("note", ""))
                    if pnl is not None:
                        pnls.append(pnl)
    except Exception as e:
        return {"error": str(e)}

    if not pnls:
        return {
            "total_entries": total_entries,
            "completed":     0,
            "message":       "No bracket-order closes recorded yet",
        }

    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    win_rate      = len(wins) / len(pnls) * 100
    avg_win       = sum(wins)   / len(wins)   if wins   else 0.0
    avg_loss      = sum(losses) / len(losses) if losses else 0.0
    total_pnl     = sum(pnls)
    profit_factor = (
        abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0
        else float("inf")
    )

    return {
        "total_entries": total_entries,
        "completed":     len(pnls),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "avg_win_pct":   round(avg_win, 2),
        "avg_loss_pct":  round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl_pct": round(total_pnl, 2),
    }


def print_stats() -> None:
    """Print a formatted performance dashboard to the log."""
    stats = get_stats()

    if "error" in stats:
        logger.info(f"[PERF] {stats['error']}")
        return

    if stats.get("completed", 0) == 0:
        logger.info(f"[PERF] {stats.get('message', 'No closed trades yet')} "
                    f"({stats.get('total_entries', 0)} entries opened)")
        return

    logger.info("=" * 55)
    logger.info("  PERFORMANCE DASHBOARD")
    logger.info("=" * 55)
    logger.info(f"  Entries opened   : {stats['total_entries']}")
    logger.info(f"  Closed trades    : {stats['completed']}  "
                f"({stats['wins']} wins / {stats['losses']} losses)")
    logger.info(f"  Win rate         : {stats['win_rate']}%  "
                f"{'✅' if stats['win_rate'] >= 45 else '⚠️ '}  (target ≥ 45%)")
    logger.info(f"  Avg win          : +{stats['avg_win_pct']}%")
    logger.info(f"  Avg loss         :  {stats['avg_loss_pct']}%")
    logger.info(f"  Profit factor    : {stats['profit_factor']}  "
                f"{'✅' if stats['profit_factor'] >= 1.5 else '⚠️ '}  (target ≥ 1.5)")
    logger.info(f"  Total closed P&L : {stats['total_pnl_pct']:+.2f}%")
    logger.info("=" * 55)


def kelly_risk_pct() -> float:
    """
    Calculate the optimal risk % per trade using the Kelly Criterion.

    Kelly formula:  f* = p - (1-p) / b
      p = win rate (probability of a winning trade)
      b = avg_win / abs(avg_loss)  (payoff ratio)

    We use half-Kelly (f* × KELLY_FRACTION) to reduce volatility.
    Returns RISK_PER_TRADE_PCT if there are fewer than KELLY_MIN_TRADES.

    This is the mathematical formula for maximising geometric / compounding growth.
    """
    from config import (
        RISK_PER_TRADE_PCT, KELLY_MIN_TRADES,
        KELLY_MIN_RISK, KELLY_MAX_RISK, KELLY_FRACTION,
    )

    stats = get_stats()
    if "error" in stats or stats.get("completed", 0) < KELLY_MIN_TRADES:
        return RISK_PER_TRADE_PCT   # Not enough data — use default

    win_rate = stats["win_rate"] / 100
    avg_win  = stats["avg_win_pct"]
    avg_loss = abs(stats["avg_loss_pct"])

    if avg_loss == 0 or avg_win == 0:
        return RISK_PER_TRADE_PCT

    b      = avg_win / avg_loss                  # payoff ratio
    kelly  = win_rate - (1 - win_rate) / b       # full Kelly fraction
    result = kelly * KELLY_FRACTION              # half-Kelly

    clamped = max(KELLY_MIN_RISK, min(KELLY_MAX_RISK, result))
    logger.info(
        f"[KELLY] win={win_rate*100:.1f}%  b={b:.2f}  "
        f"full={kelly*100:.2f}%  half={result*100:.2f}%  → using {clamped*100:.2f}%"
    )
    return clamped


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print_stats()
