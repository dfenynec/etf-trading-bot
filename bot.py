"""
ETF + Crypto Trading Bot
-------------------------
- ETFs:   runs during US market hours (Mon-Fri 9:30-16:00 ET), every 60 min
- Crypto: runs 24/7 via real-time WebSocket (1-min bars), assesses every bar

Usage:
    python bot.py
"""

import logging
import threading
import time
from datetime import datetime

import schedule

from config import (
    ETF_UNIVERSE, RUN_INTERVAL_MINUTES,
    MAX_POSITIONS,
)
from data_fetcher import fetch_all_etfs
from indicators import calculate_indicators
from risk_manager import calculate_position_size, calculate_stop_loss, calculate_take_profit, can_open_position
from strategy import rank_buy_candidates, score_etf
from trader import AlpacaTrader
from live_trader import LiveCryptoTrader

# --- Logging setup ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log"),
    ],
)
logger = logging.getLogger(__name__)

# --- Init shared trader ---
trader = AlpacaTrader()


def print_signal_table(signals: list, label: str = "") -> None:
    """Pretty-print the scoring table."""
    logger.info("-" * 80)
    logger.info(f"{'TICKER':<12} {'SCORE':>6} {'ADX':>6} {'TREND':>6} {'SIGNAL':<6} {'PRICE':>10}  TOP REASON")
    logger.info("-" * 80)
    for s in sorted(signals, key=lambda x: x["score"], reverse=True):
        top_reason = s["reasons"][0] if s["reasons"] else ""
        trend_str = "YES" if s.get("trending") else "NO"
        logger.info(
            f"{s['ticker']:<12} {s['score']:>6} {s.get('adx', 0):>6.1f} {trend_str:>6} "
            f"{s['signal']:<6} ${s['price']:>9.4f}  {top_reason}"
        )
    logger.info("-" * 80)


# ---------------------------------------------------------------------------
# ETF Strategy (market hours only)
# ---------------------------------------------------------------------------

def run_etf_strategy() -> None:
    logger.info("=" * 70)
    logger.info(f"ETF STRATEGY RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    if not trader.is_market_open():
        logger.info("Market is closed. Skipping ETF run.")
        return

    portfolio_value = trader.get_portfolio_value()
    cash = trader.get_cash()
    positions = trader.get_positions()
    # Only consider stock positions (not crypto)
    etf_positions = {k: v for k, v in positions.items() if "/" not in k}

    logger.info(f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} | ETF Positions: {len(etf_positions)}")
    logger.info(f"Analyzing {len(ETF_UNIVERSE)} ETFs...")

    all_data = fetch_all_etfs(ETF_UNIVERSE)
    signals = []
    for ticker, df in all_data.items():
        df_ind = calculate_indicators(df)
        signal = score_etf(df_ind, ticker)
        signals.append(signal)

    print_signal_table(signals, label="ETF")

    # Exit positions with SELL signal
    for ticker in list(etf_positions.keys()):
        sig = next((s for s in signals if s["ticker"] == ticker), None)
        if sig and sig["signal"] == "SELL":
            logger.info(f"  → SELL {ticker} (score {sig['score']})")
            trader.sell(ticker)
        else:
            logger.info(f"  → Hold {ticker} (score {sig['score'] if sig else 'N/A'})")

    # Refresh after sells
    positions = trader.get_positions()
    etf_positions = {k: v for k, v in positions.items() if "/" not in k}
    held = list(etf_positions.keys())
    current_count = len(etf_positions)
    cash = trader.get_cash()

    for candidate in rank_buy_candidates(signals):
        ticker = candidate["ticker"]
        ok, reason = can_open_position(current_count, ticker, held)
        if not ok:
            logger.info(f"  Skipping {ticker}: {reason}")
            continue

        price = candidate["price"]
        atr = candidate["atr"]
        qty = calculate_position_size(portfolio_value, price)
        cost = qty * price

        if cost > cash:
            logger.info(f"  Skipping {ticker}: insufficient cash (need ${cost:.2f}, have ${cash:.2f})")
            continue

        stop = calculate_stop_loss(price, atr)
        tp = calculate_take_profit(price, atr)
        logger.info(f"  → BUY {qty}x {ticker} @ ~${price:.2f} | Stop: ${stop} | Target: ${tp} | Score: {candidate['score']}")

        if trader.buy(ticker, qty):
            current_count += 1
            held.append(ticker)
            cash -= cost

        if current_count >= MAX_POSITIONS:
            break

    logger.info("ETF run complete.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Trading bot starting up...")
    logger.info(f"ETF universe ({len(ETF_UNIVERSE)}): {ETF_UNIVERSE}")
    logger.info(f"ETF interval: every {RUN_INTERVAL_MINUTES} min (market hours only)")
    logger.info("Crypto: real-time WebSocket stream (1-min bars, 24/7)")
    logger.info("Type CTRL+C to stop.\n")

    # --- Start crypto live trader in a background thread ---
    live = LiveCryptoTrader(trader)
    crypto_thread = threading.Thread(target=live.run, daemon=True, name="LiveCryptoTrader")
    crypto_thread.start()
    logger.info("LiveCryptoTrader started in background thread.\n")

    # --- Run ETF strategy immediately, then on schedule ---
    run_etf_strategy()
    schedule.every(RUN_INTERVAL_MINUTES).minutes.do(run_etf_strategy)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
