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
import math
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
TRADE_COOLDOWN = 120  # 2 minutes (was 5)

# Valid symbols set for fast lookup in the bar handler
_VALID_SYMBOLS = set(CRYPTO_UNIVERSE)


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
        self._sl_tp: dict = {}             # symbol → {stop_loss, take_profit}

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
        symbol = bar.symbol  # Alpaca crypto stream uses "BTC/USD" format
        if symbol not in _VALID_SYMBOLS:
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
        # Alpaca trading API stores crypto positions without the slash
        # ("SOLUSD"), but the WebSocket stream uses "SOL/USD". Normalize
        # to no-slash so the lookup always matches.
        alpaca_sym = symbol.replace("/", "")
        crypto_positions = {k: v for k, v in positions.items()
                            if k == alpaca_sym or k == symbol}
        holding = alpaca_sym in positions

        logger.info(
            f"[LIVE] {symbol:<10} ${price:>10.4f}  "
            f"score: {signal['score']:+d}  signal: {signal['signal']:<5}  "
            f"holding: {holding}"
        )

        # --- Stop-loss / take-profit enforcement (checked before any signal logic) ---
        if holding and alpaca_sym in self._sl_tp:
            levels = self._sl_tp[alpaca_sym]
            if price <= levels["stop_loss"]:
                logger.warning(
                    f"[LIVE] *** STOP-LOSS HIT {symbol} @ ${price:.4f} "
                    f"(stop: ${levels['stop_loss']:.4f}) ***"
                )
                if self.trader.sell_crypto(alpaca_sym):
                    self._last_traded[alpaca_sym] = time.time()
                    del self._sl_tp[alpaca_sym]
                return
            elif price >= levels["take_profit"]:
                logger.info(
                    f"[LIVE] *** TAKE-PROFIT HIT {symbol} @ ${price:.4f} "
                    f"(target: ${levels['take_profit']:.4f}) ***"
                )
                if self.trader.sell_crypto(alpaca_sym):
                    self._last_traded[alpaca_sym] = time.time()
                    del self._sl_tp[alpaca_sym]
                return

        # Enforce cooldown to prevent overtrading on the same symbol
        since_last_trade = time.time() - self._last_traded.get(alpaca_sym, 0)
        if since_last_trade < TRADE_COOLDOWN:
            return

        if signal["signal"] == "BUY" and not holding:
            if len(crypto_positions) >= MAX_CRYPTO_POSITIONS:
                logger.info(f"[LIVE] Skip {symbol}: at max positions ({MAX_CRYPTO_POSITIONS})")
                return

            portfolio_value  = self.trader.get_portfolio_value()
            crypto_bp        = self.trader.get_crypto_buying_power()
            # Apply 2% buffer: accounts for bid/ask spread + rounding.
            # Use floor (not round) so qty * price never exceeds max_dollars.
            max_dollars      = min(portfolio_value * MAX_CRYPTO_POSITION_PCT, crypto_bp * 0.98)
            qty              = math.floor(max_dollars / price * 1_000_000) / 1_000_000

            if qty <= 0:
                logger.info(f"[LIVE] Skip {symbol}: not enough cash (bp=${crypto_bp:.2f})")
                return

            stop = calculate_stop_loss(price, atr)
            tp = calculate_take_profit(price, atr)

            logger.info(
                f"[LIVE] *** BUY  {symbol} | {qty:.6f} units @ ${price:.4f} "
                f"| Stop: ${stop} | Target: ${tp} | Score: {signal['score']} ***"
            )
            if self.trader.buy_crypto(symbol, qty):
                self._sl_tp[alpaca_sym] = {"stop_loss": stop, "take_profit": tp}
                self._last_traded[alpaca_sym] = time.time()

        elif signal["signal"] == "SELL" and holding:
            logger.info(
                f"[LIVE] *** SELL {symbol} @ ${price:.4f} | Score: {signal['score']} ***"
            )
            if self.trader.sell_crypto(alpaca_sym):
                self._sl_tp.pop(alpaca_sym, None)
                self._last_traded[alpaca_sym] = time.time()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        """Start the WebSocket stream. Blocking — call from a dedicated thread."""
        # Load base data before subscribing
        self._refresh_base_data()

        for sym in CRYPTO_UNIVERSE:
            self.stream.subscribe_bars(self.on_bar, sym)

        logger.info(f"[LIVE] Streaming 1-min bars for: {CRYPTO_UNIVERSE}")
        logger.info(f"[LIVE] Trade cooldown: {TRADE_COOLDOWN}s | Refresh interval: {FULL_REFRESH_INTERVAL}s")
        self.stream.run()
