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
    RUN_INTERVAL_MINUTES, SCREEN_TOP_N_ETF, SCREEN_TOP_N_CRYPTO,
    MAX_POSITIONS, MAX_SHORT_POSITIONS, DAILY_LOSS_LIMIT_PCT,
)
from screener import screen_etfs
from data_fetcher import fetch_all_etfs
from indicators import calculate_indicators
from risk_manager import calculate_position_size, calculate_stop_loss, calculate_take_profit
from strategy import rank_buy_candidates, rank_sell_candidates, score_etf
from trader import AlpacaTrader
from live_trader import LiveCryptoTrader
from trade_journal import log_trade

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

    # Daily loss circuit breaker
    daily_pnl = trader.get_daily_pnl_pct()
    if daily_pnl < -DAILY_LOSS_LIMIT_PCT:
        logger.warning(
            f"[RISK] Daily loss limit hit ({daily_pnl*100:.2f}%) — "
            f"skipping ETF run to protect capital"
        )
        return

    portfolio_value  = trader.get_portfolio_value()
    buying_power     = trader.get_buying_power()

    # Separate long and short ETF positions
    long_positions  = {k: v for k, v in trader.get_long_positions().items()  if "/" not in k}
    short_positions = {k: v for k, v in trader.get_short_positions().items() if "/" not in k}

    logger.info(
        f"Portfolio: ${portfolio_value:,.2f} | Buying power: ${buying_power:,.2f} | "
        f"Longs: {len(long_positions)} | Shorts: {len(short_positions)}"
    )
    # Dynamic screener — ranks ~35 candidates by momentum, returns top 12
    universe = screen_etfs()
    logger.info(f"Active ETF universe ({len(universe)}): {universe}")

    all_data = fetch_all_etfs(universe)
    signals = []
    for ticker, df in all_data.items():
        df_ind = calculate_indicators(df)
        signal = score_etf(df_ind, ticker)
        signals.append(signal)

    print_signal_table(signals, label="ETF")

    # --- Manage existing long positions ---
    for ticker in list(long_positions.keys()):
        sig = next((s for s in signals if s["ticker"] == ticker), None)
        if sig and sig["signal"] == "SELL":
            logger.info(f"  → SELL LONG {ticker} (score {sig['score']})")
            if trader.sell(ticker):
                log_trade("SELL", ticker, 0, sig["price"], sig["score"], note="Signal exit")
        else:
            logger.info(f"  → Hold long {ticker} (score {sig['score'] if sig else 'N/A'})")

    # --- Manage existing short positions ---
    for ticker in list(short_positions.keys()):
        sig = next((s for s in signals if s["ticker"] == ticker), None)
        if sig and sig["signal"] == "BUY":
            logger.info(f"  → COVER SHORT {ticker} (score {sig['score']})")
            if trader.cover(ticker):
                log_trade("COVER", ticker, 0, sig["price"], sig["score"], note="Signal exit")
        else:
            logger.info(f"  → Hold short {ticker} (score {sig['score'] if sig else 'N/A'})")

    # Refresh positions and buying power after exits
    long_positions  = {k: v for k, v in trader.get_long_positions().items()  if "/" not in k}
    short_positions = {k: v for k, v in trader.get_short_positions().items() if "/" not in k}
    buying_power    = trader.get_buying_power()

    # --- Open new LONG positions ---
    for candidate in rank_buy_candidates(signals):
        ticker = candidate["ticker"]
        if ticker in long_positions:
            continue
        if len(long_positions) >= MAX_POSITIONS:
            break

        price = candidate["price"]
        atr   = candidate["atr"]
        score = candidate["score"]
        qty   = calculate_position_size(portfolio_value, price, score=score, atr=atr)
        cost  = qty * price

        if cost > buying_power:
            logger.info(f"  Skip long {ticker}: insufficient buying power (need ${cost:.2f}, have ${buying_power:.2f})")
            continue

        stop = calculate_stop_loss(price, atr)
        tp   = calculate_take_profit(price, atr)
        logger.info(f"  → BUY  {qty}x {ticker} @ ~${price:.4f} | Stop: ${stop} | Target: ${tp} | Score: {score}")

        if trader.buy(ticker, qty, stop_loss=stop, take_profit=tp):
            long_positions[ticker] = None
            buying_power -= cost
            log_trade("BUY", ticker, qty, price, score, stop, tp)

    # --- Open new SHORT positions ---
    for candidate in rank_sell_candidates(signals):
        ticker = candidate["ticker"]
        if ticker in short_positions:
            continue
        if len(short_positions) >= MAX_SHORT_POSITIONS:
            break

        price = candidate["price"]
        atr   = candidate["atr"]
        score = candidate["score"]
        qty   = calculate_position_size(portfolio_value, price, score=score, atr=atr)
        cost  = qty * price

        if cost > buying_power:
            logger.info(f"  Skip short {ticker}: insufficient buying power (need ${cost:.2f}, have ${buying_power:.2f})")
            continue

        stop = calculate_take_profit(price, atr)  # Stop is ABOVE entry for shorts
        tp   = calculate_stop_loss(price, atr)     # Target is BELOW entry for shorts
        logger.info(f"  → SHORT {qty}x {ticker} @ ~${price:.4f} | Stop: ${stop} | Target: ${tp} | Score: {score}")

        if trader.short(ticker, qty, stop_loss=stop, take_profit=tp):
            short_positions[ticker] = None
            buying_power -= cost
            log_trade("SHORT", ticker, qty, price, score, stop, tp)

    logger.info("ETF run complete.\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    from screener import ETF_CANDIDATES, CRYPTO_CANDIDATES
    logger.info("Trading bot starting up...")
    logger.info(f"ETF candidates: {len(ETF_CANDIDATES)} → screened to top {SCREEN_TOP_N_ETF} by momentum each run")
    logger.info(f"Crypto candidates: {len(CRYPTO_CANDIDATES)} → screened to top {SCREEN_TOP_N_CRYPTO} by momentum")
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
