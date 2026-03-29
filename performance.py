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


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print_stats()
