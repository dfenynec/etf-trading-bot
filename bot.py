"""
ETF + Crypto Trading Bot
-------------------------
- ETFs: runs during US market hours (Mon-Fri 9:30-16:00 ET), every 60 min
- Crypto: runs 24/7, every 30 min
Both use the same multi-indicator scoring system.

Usage:
    python bot.py
"""

import logging
import time
from datetime import datetime

import schedule

from config import (
    ETF_UNIVERSE, RUN_INTERVAL_MINUTES,
    CRYPTO_UNIVERSE, CRYPTO_RUN_INTERVAL_MINUTES,
    MAX_POSITIONS, MAX_CRYPTO_POSITIONS, MAX_CRYPTO_POSITION_PCT,
)
from data_fetcher import fetch_all_etfs, fetch_all_crypto
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


def print_signal_table(signals: list, label: str = "") -> None:
    """Pretty-print the scoring table."""
    logger.info("-" * 70)
    logger.info(f"{'TICKER':<12} {'SCORE':>6} {'SIGNAL':<8} {'PRICE':>10}  TOP REASON")
    logger.info("-" * 70)
    for s in sorted(signals, key=lambda x: x["score"], reverse=True):
        top_reason = s["reasons"][0] if s["reasons"] else ""
        logger.info(
            f"{s['ticker']:<12} {s['score']:>6} {s['signal']:<8} ${s['price']:>9.2f}  {top_reason}"
        )
    logger.info("-" * 70)


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
# Crypto Strategy (24/7)
# ---------------------------------------------------------------------------

def run_crypto_strategy() -> None:
    logger.info("=" * 70)
    logger.info(f"CRYPTO STRATEGY RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 70)

    portfolio_value = trader.get_portfolio_value()
    cash = trader.get_cash()
    positions = trader.get_positions()
    # Crypto positions contain "/" in the symbol
    crypto_positions = {k: v for k, v in positions.items() if "/" in k}

    logger.info(f"Portfolio: ${portfolio_value:,.2f} | Cash: ${cash:,.2f} | Crypto Positions: {len(crypto_positions)}")
    logger.info(f"Analyzing {len(CRYPTO_UNIVERSE)} crypto pairs...")

    all_data = fetch_all_crypto(CRYPTO_UNIVERSE)
    signals = []
    for symbol, df in all_data.items():
        df_ind = calculate_indicators(df)
        signal = score_etf(df_ind, symbol)  # Same scoring logic works for crypto
        signals.append(signal)

    print_signal_table(signals, label="CRYPTO")

    # Exit crypto positions with SELL signal
    for symbol in list(crypto_positions.keys()):
        sig = next((s for s in signals if s["ticker"] == symbol), None)
        if sig and sig["signal"] == "SELL":
            logger.info(f"  → SELL {symbol} (score {sig['score']})")
            trader.sell_crypto(symbol)
        else:
            logger.info(f"  → Hold {symbol} (score {sig['score'] if sig else 'N/A'})")

    # Refresh after sells
    positions = trader.get_positions()
    crypto_positions = {k: v for k, v in positions.items() if "/" in k}
    held = list(crypto_positions.keys())
    current_count = len(crypto_positions)
    cash = trader.get_cash()

    for candidate in rank_buy_candidates(signals):
        symbol = candidate["ticker"]
        ok, reason = can_open_position(current_count, symbol, held)
        if not ok:
            logger.info(f"  Skipping {symbol}: {reason}")
            continue

        price = candidate["price"]
        atr = candidate["atr"]
        max_dollars = portfolio_value * MAX_CRYPTO_POSITION_PCT
        qty = round(max_dollars / price, 6)  # Fractional crypto
        cost = qty * price

        if cost > cash:
            logger.info(f"  Skipping {symbol}: insufficient cash (need ${cost:.2f}, have ${cash:.2f})")
            continue

        stop = calculate_stop_loss(price, atr)
        tp = calculate_take_profit(price, atr)
        logger.info(f"  → BUY {qty:.6f}x {symbol} @ ~${price:.2f} | Stop: ${stop} | Target: ${tp} | Score: {candidate['score']}")

        if trader.buy_crypto(symbol, qty):
            current_count += 1
            held.append(symbol)
            cash -= cost

        if current_count >= MAX_CRYPTO_POSITIONS:
            break

    logger.info("Crypto run complete.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    logger.info("Trading bot starting up...")
    logger.info(f"ETF universe ({len(ETF_UNIVERSE)}): {ETF_UNIVERSE}")
    logger.info(f"Crypto universe ({len(CRYPTO_UNIVERSE)}): {CRYPTO_UNIVERSE}")
    logger.info(f"ETF interval: every {RUN_INTERVAL_MINUTES} min (market hours only)")
    logger.info(f"Crypto interval: every {CRYPTO_RUN_INTERVAL_MINUTES} min (24/7)")
    logger.info("Type CTRL+C to stop.\n")

    # Run both immediately on startup
    run_etf_strategy()
    run_crypto_strategy()

    # Schedule both
    schedule.every(RUN_INTERVAL_MINUTES).minutes.do(run_etf_strategy)
    schedule.every(CRYPTO_RUN_INTERVAL_MINUTES).minutes.do(run_crypto_strategy)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
