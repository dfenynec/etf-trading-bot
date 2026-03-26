"""
ETF Trading Bot
---------------
Runs on a schedule during market hours.
Scores ETFs with a multi-indicator system and auto-trades via Alpaca.

Usage:
    python bot.py
"""

import logging
import time
from datetime import datetime

import schedule

from config import ETF_UNIVERSE, RUN_INTERVAL_MINUTES
from data_fetcher import fetch_all_etfs
from indicators import calculate_indicators
from risk_manager import calculate_position_size, calculate_stop_loss, calculate_take_profit, can_open_position
from strategy import rank_buy_candidates, score_etf
from trader import AlpacaTrader

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

# --- Init trader once ---
trader = AlpacaTrader()


def print_signal_table(signals: list) -> None:
    """Pretty-print the scoring table for all ETFs."""
    logger.info("-" * 70)
    logger.info(f"{'TICKER':<8} {'SCORE':>6} {'SIGNAL':<8} {'PRICE':>8}  TOP REASON")
    logger.info("-" * 70)
    for s in sorted(signals, key=lambda x: x["score"], reverse=True):
        top_reason = s["reasons"][0] if s["reasons"] else ""
        logger.info(
            f"{s['ticker']:<8} {s['score']:>6} {s['signal']:<8} ${s['price']:>7.2f}  {top_reason}"
        )
    logger.info("-" * 70)


def run_strategy() -> None:
    logger.info("=" * 70)
    logger.info(f"STRATEGY RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    # --- Market hours check ---
    if not trader.is_market_open():
        logger.info("Market is closed. Waiting for next run.")
        return

    # --- Account snapshot ---
    portfolio_value = trader.get_portfolio_value()
    cash = trader.get_cash()
    positions = trader.get_positions()
    held_tickers = list(positions.keys())

    logger.info(f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} | Positions: {len(positions)}")

    # --- Fetch & score all ETFs ---
    logger.info(f"Analyzing {len(ETF_UNIVERSE)} ETFs...")
    all_data = fetch_all_etfs(ETF_UNIVERSE)

    signals = []
    for ticker, df in all_data.items():
        df_ind = calculate_indicators(df)
        signal = score_etf(df_ind, ticker)
        signals.append(signal)

    print_signal_table(signals)

    # --- Step 1: Exit positions with SELL signal ---
    logger.info("Checking held positions for exit signals...")
    for ticker in list(positions.keys()):
        ticker_signal = next((s for s in signals if s["ticker"] == ticker), None)
        if ticker_signal and ticker_signal["signal"] == "SELL":
            logger.info(f"  → SELL signal on held {ticker} (score {ticker_signal['score']})")
            trader.sell(ticker)  # Closes full position
        else:
            logger.info(f"  → Holding {ticker} (score {ticker_signal['score'] if ticker_signal else 'N/A'})")

    # --- Step 2: Enter new positions with BUY signal ---
    # Refresh positions after any sells
    positions = trader.get_positions()
    held_tickers = list(positions.keys())
    current_count = len(positions)
    cash = trader.get_cash()

    buy_candidates = rank_buy_candidates(signals)
    logger.info(f"BUY candidates: {[c['ticker'] for c in buy_candidates]}")

    for candidate in buy_candidates:
        ticker = candidate["ticker"]

        # Check eligibility
        ok, reason = can_open_position(current_count, ticker, held_tickers)
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

        logger.info(
            f"  → BUY {qty}x {ticker} @ ~${price:.2f} | "
            f"Stop: ${stop} | Target: ${tp} | Score: {candidate['score']}"
        )

        success = trader.buy(ticker, qty)
        if success:
            current_count += 1
            held_tickers.append(ticker)
            cash -= cost

    logger.info("Run complete.\n")


def main() -> None:
    logger.info("Trading bot starting up...")
    logger.info(f"ETF universe: {ETF_UNIVERSE}")
    logger.info(f"Run interval: every {RUN_INTERVAL_MINUTES} minutes")
    logger.info("Type CTRL+C to stop.\n")

    # Run immediately on startup
    run_strategy()

    # Then run on schedule
    schedule.every(RUN_INTERVAL_MINUTES).minutes.do(run_strategy)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
