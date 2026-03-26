"""
Real-time crypto trader using Alpaca's WebSocket stream.
Subscribes to 1-minute bars and assesses signals on every new bar —
reacting 30x faster than the original 30-minute scheduled strategy.

Flow per bar:
  1. Update the latest close price in the cached daily data
  2. Recalculate all technical indicators
  3. Score the signal (-7 to +7)
  4. Trade immediately if signal crosses threshold (with 5-min cooldown)
  5. Every 30 min: refresh base data from yfinance in background
"""
import threading
import time
import logging

from alpaca.data.live import CryptoDataStream

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    CRYPTO_UNIVERSE, MAX_CRYPTO_POSITIONS, MAX_CRYPTO_POSITION_PCT,
)
from data_fetcher import fetch_all_crypto
from indicators import calculate_indicators
from strategy import score_etf
from risk_manager import calculate_stop_loss, calculate_take_profit
from trader import AlpacaTrader

logger = logging.getLogger(__name__)

# How often to pull fresh daily data from yfinance (seconds)
FULL_REFRESH_INTERVAL = 1800  # 30 minutes

# Minimum seconds between trades on the same symbol (avoids thrashing)
TRADE_COOLDOWN = 300  # 5 minutes

# Map Alpaca WebSocket format (BTCUSD) → internal format (BTC/USD)
_WS_TO_SYMBOL = {s.replace("/", ""): s for s in CRYPTO_UNIVERSE}


class LiveCryptoTrader:
    """
    Streams real-time 1-minute crypto bars from Alpaca.
    On each bar: updates the latest price, recalculates indicators, trades on signal.
    """

    def __init__(self, trader: AlpacaTrader):
        self.trader = trader
        self.stream = CryptoDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self._base_data: dict = {}          # symbol → DataFrame (daily OHLCV)
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._refreshing = False            # Prevent concurrent refreshes
        self._last_traded: dict = {}        # symbol → timestamp of last trade

    # ------------------------------------------------------------------
    # Data management
    # ------------------------------------------------------------------

    def _refresh_base_data(self):
        """Pull fresh daily history from yfinance. Runs in a background thread."""
        if self._refreshing:
            return
        self._refreshing = True
        try:
            logger.info("[LIVE] Refreshing base data from yfinance...")
            fresh = fetch_all_crypto(CRYPTO_UNIVERSE)
            with self._lock:
                self._base_data = fresh
            self._last_refresh = time.time()
            logger.info(f"[LIVE] Base data ready: {list(fresh.keys())}")
        except Exception as e:
            logger.error(f"[LIVE] Base data refresh failed: {e}")
        finally:
            self._refreshing = False

    def _update_latest_close(self, symbol: str, close: float) -> None:
        """
        Overwrite the last daily row's close with the real-time bar close.
        This makes RSI, MACD, and Bollinger Bands reflect the current price
        rather than yesterday's close.
        """
        with self._lock:
            df = self._base_data.get(symbol)
            if df is not None and not df.empty:
                self._base_data[symbol].at[df.index[-1], "close"] = close

    def _get_signal(self, symbol: str) -> dict | None:
        with self._lock:
            df = self._base_data.get(symbol)
        if df is None or df.empty:
            return None
        df_ind = calculate_indicators(df.copy())
        if df_ind.empty:
            return None
        return score_etf(df_ind, symbol)

    # ------------------------------------------------------------------
    # WebSocket handler — fires on every 1-minute bar close
    # ------------------------------------------------------------------

    async def on_bar(self, bar):
        symbol = _WS_TO_SYMBOL.get(bar.symbol)
        if not symbol:
            return

        # Trigger background refresh if data is stale
        if time.time() - self._last_refresh > FULL_REFRESH_INTERVAL and not self._refreshing:
            threading.Thread(target=self._refresh_base_data, daemon=True).start()

        # Inject real-time close price into the cached daily data
        self._update_latest_close(symbol, float(bar.close))

        signal = self._get_signal(symbol)
        if not signal:
            return

        price = float(bar.close)
        atr = signal["atr"]

        positions = self.trader.get_positions()
        crypto_positions = {k: v for k, v in positions.items() if "/" in k}
        holding = symbol in crypto_positions

        logger.info(
            f"[LIVE] {symbol:<10} ${price:>10.4f}  "
            f"score: {signal['score']:+d}  signal: {signal['signal']:<5}  "
            f"holding: {holding}"
        )

        # Enforce cooldown to prevent overtrading on the same symbol
        since_last_trade = time.time() - self._last_traded.get(symbol, 0)
        if since_last_trade < TRADE_COOLDOWN:
            return

        if signal["signal"] == "BUY" and not holding:
            if len(crypto_positions) >= MAX_CRYPTO_POSITIONS:
                logger.info(f"[LIVE] Skip {symbol}: at max positions ({MAX_CRYPTO_POSITIONS})")
                return

            portfolio_value = self.trader.get_portfolio_value()
            cash = self.trader.get_cash()
            qty = round((portfolio_value * MAX_CRYPTO_POSITION_PCT) / price, 6)

            if qty * price > cash:
                logger.info(f"[LIVE] Skip {symbol}: not enough cash")
                return

            stop = calculate_stop_loss(price, atr)
            tp = calculate_take_profit(price, atr)

            logger.info(
                f"[LIVE] *** BUY  {symbol} | {qty:.6f} units @ ${price:.4f} "
                f"| Stop: ${stop} | Target: ${tp} | Score: {signal['score']} ***"
            )
            if self.trader.buy_crypto(symbol, qty):
                self._last_traded[symbol] = time.time()

        elif signal["signal"] == "SELL" and holding:
            logger.info(
                f"[LIVE] *** SELL {symbol} @ ${price:.4f} | Score: {signal['score']} ***"
            )
            if self.trader.sell_crypto(symbol):
                self._last_traded[symbol] = time.time()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        """Start the WebSocket stream. Blocking — call from a dedicated thread."""
        # Load base data before subscribing
        self._refresh_base_data()

        ws_symbols = [s.replace("/", "") for s in CRYPTO_UNIVERSE]
        for ws_sym in ws_symbols:
            self.stream.subscribe_bars(self.on_bar, ws_sym)

        logger.info(f"[LIVE] Streaming 1-min bars for: {CRYPTO_UNIVERSE}")
        logger.info(f"[LIVE] Trade cooldown: {TRADE_COOLDOWN}s | Refresh interval: {FULL_REFRESH_INTERVAL}s")
        self.stream.run()
