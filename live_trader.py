"""
Real-time crypto trader using Alpaca's WebSocket stream.

Improvements in this version:
  1. Daily loss circuit breaker  — halt new trades if P&L < -3% today
  2. BTC correlation filter      — suppress altcoin BUYs when BTC is bearish
  3. Trade journal               — every trade logged to trade_journal.csv
  4. Breakeven stop              — move stop to entry once 1x ATR in profit
  5. Position cache              — positions fetched max once per 30s (not per bar)
  6. Hard HOLD on low ADX        — handled in strategy.py (no more score halving)
  7. Intraday high/low update    — ATR uses real-time range, not just yesterday's

Flow per bar:
  1. Inject real-time bar (close + high/low) into cached daily data
  2. Recalculate all indicators
  3. Check breakeven for held positions
  4. Check daily loss limit before any new trade
  5. BTC correlation filter before altcoin BUYs
  6. Trade on signal if all checks pass
  7. Refresh base data from yfinance every 30 min in background
"""
import math
import threading
import time
import logging

from alpaca.data.live import CryptoDataStream

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    MAX_CRYPTO_POSITIONS, MAX_CRYPTO_POSITION_PCT,
    DAILY_LOSS_LIMIT_PCT, BREAKEVEN_ATR_TRIGGER,
    BTC_CORRELATION_FILTER, POSITION_CACHE_TTL,
)
from screener import CRYPTO_CANDIDATES, screen_crypto
from data_fetcher import fetch_all_crypto
from indicators import calculate_indicators
from strategy import score_etf
from risk_manager import calculate_stop_loss, calculate_take_profit
from trade_journal import log_trade
from trader import AlpacaTrader

logger = logging.getLogger(__name__)

FULL_REFRESH_INTERVAL = 1800   # Refresh yfinance daily data every 30 min
TRADE_COOLDOWN        = 300    # Min seconds between trades on same symbol
PNL_CHECK_INTERVAL    = 300    # Re-check daily P&L every 5 min

_ALL_CANDIDATE_SYMS  = set(CRYPTO_CANDIDATES)
_ALPACA_CRYPTO_SYMS  = {s.replace("/", "") for s in _ALL_CANDIDATE_SYMS}


class LiveCryptoTrader:

    def __init__(self, trader: AlpacaTrader):
        self.trader      = trader
        self.stream      = CryptoDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        self._base_data  = {}           # symbol → daily OHLCV DataFrame
        self._lock       = threading.Lock()

        # Data refresh
        self._last_refresh   = 0.0
        self._refreshing     = False
        self._active_symbols: set = set()   # Top N by momentum (updated each refresh)

        # Trade timing
        self._last_traded: dict = {}    # alpaca_sym → last trade timestamp

        # Breakeven tracking: alpaca_sym → {price, atr, breakeven_set}
        self._entries: dict = {}

        # Position cache
        self._pos_cache      = {}
        self._pos_cache_time = 0.0

        # Daily loss circuit breaker
        self._daily_halt      = False
        self._pnl_cache_time  = 0.0

    # ------------------------------------------------------------------
    # Data management
    # ------------------------------------------------------------------

    def _refresh_base_data(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            logger.info(f"[LIVE] Refreshing base data for {len(CRYPTO_CANDIDATES)} candidates...")
            fresh = fetch_all_crypto(CRYPTO_CANDIDATES)
            with self._lock:
                self._base_data = fresh
            # Re-rank and update the active trading set
            ranked = screen_crypto(fresh)
            self._active_symbols = set(ranked)
            self._last_refresh = time.time()
            logger.info(f"[LIVE] Base data ready. Active symbols: {ranked}")
        except Exception as e:
            logger.error(f"[LIVE] Base data refresh failed: {e}")
        finally:
            self._refreshing = False

    def _update_latest_bar(self, symbol: str, bar) -> None:
        """
        Inject real-time 1-min bar into the cached daily data.
        Updates close AND expands high/low so ATR reflects intraday range.
        """
        with self._lock:
            df = self._base_data.get(symbol)
            if df is None or df.empty:
                return
            idx = df.index[-1]
            self._base_data[symbol].at[idx, "close"] = float(bar.close)
            if float(bar.high) > self._base_data[symbol].at[idx, "high"]:
                self._base_data[symbol].at[idx, "high"] = float(bar.high)
            if float(bar.low) < self._base_data[symbol].at[idx, "low"]:
                self._base_data[symbol].at[idx, "low"] = float(bar.low)

    def _get_signal(self, symbol: str) -> dict | None:
        with self._lock:
            df = self._base_data.get(symbol)
        if df is None or df.empty:
            return None
        df_ind = calculate_indicators(df.copy())
        if df_ind.empty:
            return None
        return score_etf(df_ind, symbol)

    def _get_btc_signal(self) -> dict | None:
        """BTC signal used by the correlation filter."""
        with self._lock:
            df = self._base_data.get("BTC/USD")
        if df is None:
            return None
        df_ind = calculate_indicators(df.copy())
        if df_ind.empty:
            return None
        return score_etf(df_ind, "BTC/USD")

    # ------------------------------------------------------------------
    # Position cache (reduces get_positions() API calls)
    # ------------------------------------------------------------------

    def _get_cached_positions(self) -> dict:
        if time.time() - self._pos_cache_time > POSITION_CACHE_TTL:
            self._pos_cache      = self.trader.get_positions()
            self._pos_cache_time = time.time()
        return self._pos_cache

    def _invalidate_pos_cache(self) -> None:
        """Force a fresh fetch on the next bar after a trade."""
        self._pos_cache_time = 0.0

    # ------------------------------------------------------------------
    # Daily loss circuit breaker
    # ------------------------------------------------------------------

    def _check_daily_halt(self) -> bool:
        """
        Returns True if the daily loss limit has been breached.
        Re-checks Alpaca P&L every PNL_CHECK_INTERVAL seconds.
        """
        if time.time() - self._pnl_cache_time > PNL_CHECK_INTERVAL:
            pnl = self.trader.get_daily_pnl_pct()
            self._pnl_cache_time = time.time()
            if pnl < -DAILY_LOSS_LIMIT_PCT:
                if not self._daily_halt:
                    logger.warning(
                        f"[RISK] Daily loss limit hit ({pnl*100:.2f}%) — "
                        f"halting all new trades for today"
                    )
                self._daily_halt = True
            else:
                if self._daily_halt:
                    logger.info(f"[RISK] Daily P&L recovered ({pnl*100:.2f}%) — trading resumed")
                self._daily_halt = False
        return self._daily_halt

    # ------------------------------------------------------------------
    # Breakeven stop
    # ------------------------------------------------------------------

    def _check_breakeven(self, alpaca_sym: str, price: float) -> None:
        """
        Once price is BREAKEVEN_ATR_TRIGGER × ATR above entry,
        move the stop order to entry price — ensuring the trade can't lose.
        """
        entry = self._entries.get(alpaca_sym)
        if not entry or entry["breakeven_set"]:
            return
        trigger_price = entry["price"] + BREAKEVEN_ATR_TRIGGER * entry["atr"]
        if price >= trigger_price:
            logger.info(
                f"[LIVE] Breakeven trigger: {alpaca_sym} @ ${price:.4f} "
                f"(entry ${entry['price']:.4f} + {BREAKEVEN_ATR_TRIGGER}x ATR)"
            )
            if self.trader.move_stop_to_breakeven(alpaca_sym, entry["price"]):
                entry["breakeven_set"] = True

    # ------------------------------------------------------------------
    # WebSocket handler — fires on every 1-minute bar close
    # ------------------------------------------------------------------

    async def on_bar(self, bar):
        symbol = bar.symbol
        # Only process top-ranked symbols (screener updates every 30 min)
        if symbol not in self._active_symbols:
            return

        # Background refresh if daily data is stale
        if time.time() - self._last_refresh > FULL_REFRESH_INTERVAL and not self._refreshing:
            threading.Thread(target=self._refresh_base_data, daemon=True).start()

        # Inject real-time bar (close + intraday high/low)
        self._update_latest_bar(symbol, bar)

        signal = self._get_signal(symbol)
        if not signal:
            return

        price      = float(bar.close)
        atr        = signal["atr"]
        alpaca_sym = symbol.replace("/", "")

        # Cached positions — 1 API call per POSITION_CACHE_TTL seconds
        positions        = self._get_cached_positions()
        holding          = alpaca_sym in positions
        crypto_positions = {k: v for k, v in positions.items() if k in _ALPACA_CRYPTO_SYMS}

        logger.info(
            f"[LIVE] {symbol:<10} ${price:>10.4f}  "
            f"score: {signal['score']:+d}  signal: {signal['signal']:<5}  "
            f"holding: {holding}"
        )

        # Breakeven check for open positions
        if holding:
            self._check_breakeven(alpaca_sym, price)

        # Cooldown — prevent signal-flip overtrading
        if time.time() - self._last_traded.get(alpaca_sym, 0) < TRADE_COOLDOWN:
            return

        # ---- BUY --------------------------------------------------------
        if signal["signal"] == "BUY" and not holding:

            # Daily loss circuit breaker
            if self._check_daily_halt():
                return

            # BTC correlation filter — don't buy altcoins in a BTC downtrend
            if BTC_CORRELATION_FILTER and symbol != "BTC/USD":
                btc_sig = self._get_btc_signal()
                if btc_sig and btc_sig["score"] < 0:
                    logger.info(
                        f"[LIVE] Skip {symbol}: BTC score {btc_sig['score']} "
                        f"(correlation filter active)"
                    )
                    return

            if len(crypto_positions) >= MAX_CRYPTO_POSITIONS:
                logger.info(f"[LIVE] Skip {symbol}: at max positions ({MAX_CRYPTO_POSITIONS})")
                return

            portfolio_value = self.trader.get_portfolio_value()
            crypto_bp       = self.trader.get_crypto_buying_power()
            max_dollars     = min(portfolio_value * MAX_CRYPTO_POSITION_PCT, crypto_bp * 0.98)
            qty             = math.floor(max_dollars / price * 1_000_000) / 1_000_000

            if qty <= 0:
                logger.info(f"[LIVE] Skip {symbol}: not enough cash (bp=${crypto_bp:.2f})")
                return

            stop = calculate_stop_loss(price, atr)
            tp   = calculate_take_profit(price, atr)

            logger.info(
                f"[LIVE] *** BUY  {symbol} | {qty:.6f} @ ${price:.4f} "
                f"| Stop: ${stop} | Target: ${tp} | Score: {signal['score']} ***"
            )
            if self.trader.buy_crypto(symbol, qty, stop_loss=stop, take_profit=tp):
                self._last_traded[alpaca_sym] = time.time()
                self._entries[alpaca_sym]     = {"price": price, "atr": atr, "breakeven_set": False}
                self._invalidate_pos_cache()
                log_trade("BUY", symbol, qty, price, signal["score"], stop, tp)

        # ---- SELL -------------------------------------------------------
        elif signal["signal"] == "SELL" and holding:
            logger.info(f"[LIVE] *** SELL {symbol} @ ${price:.4f} | Score: {signal['score']} ***")
            if self.trader.sell_crypto(alpaca_sym):
                self._last_traded[alpaca_sym] = time.time()
                self._entries.pop(alpaca_sym, None)
                self._invalidate_pos_cache()
                log_trade("SELL", symbol, 0, price, signal["score"], note="Signal exit")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._refresh_base_data()

        # Subscribe to ALL candidates — on_bar filters to _active_symbols only
        for sym in CRYPTO_CANDIDATES:
            self.stream.subscribe_bars(self.on_bar, sym)

        logger.info(f"[LIVE] Streaming 1-min bars for {len(CRYPTO_CANDIDATES)} candidates")
        logger.info(
            f"[LIVE] Cooldown: {TRADE_COOLDOWN}s | "
            f"Daily loss limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | "
            f"Breakeven trigger: {BREAKEVEN_ATR_TRIGGER}x ATR | "
            f"BTC filter: {BTC_CORRELATION_FILTER}"
        )
        self.stream.run()
