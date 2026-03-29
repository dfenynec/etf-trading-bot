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
import json
import math
import os
import threading
import time
import logging

from alpaca.data.live import CryptoDataStream

from config import (
    ALPACA_API_KEY, ALPACA_SECRET_KEY,
    MAX_CRYPTO_POSITIONS,
    DAILY_LOSS_LIMIT_PCT, TRAILING_STOP_PCT,
    BTC_CORRELATION_FILTER, POSITION_CACHE_TTL,
    PYRAMID_TRIGGER_PCT, PYRAMID_ADD_PCT,
    LOSS_THROTTLE_AFTER, RISK_PER_TRADE_PCT,
)
from screener import CRYPTO_CANDIDATES, screen_crypto
from data_fetcher import fetch_all_crypto
from indicators import calculate_indicators
from strategy import score_etf
from risk_manager import calculate_stop_loss, calculate_take_profit, calculate_crypto_position_size
from trade_journal import log_trade
from trader import AlpacaTrader
from performance import kelly_risk_pct
from learner import is_symbol_blacklisted, print_learned_state

logger = logging.getLogger(__name__)

FULL_REFRESH_INTERVAL = 1800   # Refresh yfinance daily data every 30 min
TRADE_COOLDOWN        = 300    # Min seconds between trades on same symbol
PNL_CHECK_INTERVAL    = 300    # Re-check daily P&L every 5 min
ENTRIES_FILE          = "open_entries.json"  # Persists stop/trail data across redeploys

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

        # Entry tracking: alpaca_sym → {price, atr, stop, peak_price, ...}
        # Persisted to ENTRIES_FILE so stops survive redeploys
        self._entries: dict = self._load_entries()

        # Position cache
        self._pos_cache      = {}
        self._pos_cache_time = 0.0

        # Daily loss circuit breaker
        self._daily_halt      = False
        self._pnl_cache_time  = 0.0

        # Kelly criterion — refreshed every hour from trade journal
        self._kelly_risk_pct  = RISK_PER_TRADE_PCT
        self._kelly_updated   = 0.0

        # Consecutive loss protection
        self._consecutive_losses = 0    # resets to 0 after a winning trade
        self._loss_multiplier    = 1.0  # halved after LOSS_THROTTLE_AFTER losses

    # ------------------------------------------------------------------
    # Entry persistence — stops survive redeploys
    # ------------------------------------------------------------------

    def _load_entries(self) -> dict:
        """Load open position entries from disk. Returns empty dict if file missing."""
        if not os.path.exists(ENTRIES_FILE):
            return {}
        try:
            with open(ENTRIES_FILE) as f:
                entries = json.load(f)
            if entries:
                logger.info(
                    f"[LIVE] Restored {len(entries)} open entries from disk: "
                    f"{list(entries.keys())}"
                )
            return entries
        except Exception as e:
            logger.error(f"[LIVE] Failed to load entries: {e}")
            return {}

    def _save_entries(self) -> None:
        """Persist current entries to disk. Called after every open/close."""
        try:
            with open(ENTRIES_FILE, "w") as f:
                json.dump(self._entries, f, indent=2)
        except Exception as e:
            logger.error(f"[LIVE] Failed to save entries: {e}")

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

        Safety: only update if the bar falls on the same day as the last row.
        If midnight has passed and yfinance hasn't refreshed yet, skip the
        update to avoid corrupting yesterday's row with today's prices.
        """
        with self._lock:
            df = self._base_data.get(symbol)
            if df is None or df.empty:
                return

            idx = df.index[-1]
            bar_date  = bar.timestamp.date() if hasattr(bar, 'timestamp') else None
            row_date  = idx.date() if hasattr(idx, 'date') else None

            # Skip if the day rolled over — wait for the next yfinance refresh
            if bar_date and row_date and bar_date != row_date:
                return

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
            # Detect positions closed by bracket orders since last refresh
            self._reconcile_closed_positions(self._pos_cache)
        return self._pos_cache

    def _invalidate_pos_cache(self) -> None:
        """Force a fresh fetch on the next bar after a trade."""
        self._pos_cache_time = 0.0

    def _reconcile_closed_positions(self, positions: dict) -> None:
        """
        Compare _entries (positions we opened) against current live positions.
        Any symbol in _entries that is no longer in positions was closed
        externally (e.g. manually on Alpaca dashboard, or during a restart).
        Cleans up stale _entries and logs the close to the trade journal.
        """
        for alpaca_sym in list(self._entries.keys()):
            if alpaca_sym in positions:
                continue  # Still open — nothing to do

            entry = self._entries.pop(alpaca_sym)

            # Resolve slash symbol for data lookup (e.g. "SOLUSD" → "SOL/USD")
            slash_sym = next(
                (s for s in _ALL_CANDIDATE_SYMS if s.replace("/", "") == alpaca_sym),
                alpaca_sym,
            )

            # Best-effort exit price from cached daily data
            with self._lock:
                df = self._base_data.get(slash_sym)
            exit_price = float(df["close"].iloc[-1]) if df is not None and not df.empty else 0.0

            pnl_pct = (
                (exit_price - entry["price"]) / entry["price"] * 100
                if entry["price"] > 0 else 0.0
            )
            result = "TP hit" if pnl_pct > 0 else "SL hit"

            logger.info(
                f"[LIVE] *** CLOSED {slash_sym} via bracket order ({result}) | "
                f"Entry: ${entry['price']:.4f} → Exit: ~${exit_price:.4f} | "
                f"PnL: {pnl_pct:+.2f}% ***"
            )
            log_trade(
                "CLOSE", slash_sym, 0, exit_price, 0,
                note=f"{result} | entry=${entry['price']:.4f} | pnl={pnl_pct:+.2f}%",
            )
            self._save_entries()

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
    # Kelly criterion — dynamic risk % updated hourly from trade journal
    # ------------------------------------------------------------------

    def _get_kelly_risk(self) -> float:
        """
        Return the Kelly-optimal risk fraction, refreshed every hour.
        Falls back to RISK_PER_TRADE_PCT if fewer than KELLY_MIN_TRADES exist.
        """
        if time.time() - self._kelly_updated > 3600:
            self._kelly_risk_pct = kelly_risk_pct()
            self._kelly_updated  = time.time()
        # Apply consecutive-loss multiplier on top of Kelly sizing
        return self._kelly_risk_pct * self._loss_multiplier

    # ------------------------------------------------------------------
    # Trailing stop + pyramiding (Alpaca disallows crypto bracket orders)
    # ------------------------------------------------------------------

    def _check_exit_conditions(self, alpaca_sym: str, symbol: str, price: float) -> bool:
        """
        Trailing stop + pyramiding — fires on every 1-min bar for held positions.

        Exit logic:
          1. Track peak_price since entry.
          2. Trailing stop = peak_price × (1 - TRAILING_STOP_PCT) — only moves up.
          3. Effective stop = max(initial_stop, trailing_stop).
          4. Close when price ≤ effective_stop. Updates loss/win streak counter.

        Pyramiding logic:
          Once position is up PYRAMID_TRIGGER_PCT (3%), add PYRAMID_ADD_PCT (50%)
          more units and move the initial stop to breakeven.  Done once per trade.

        Returns True if the position was closed (caller skips buy/sell logic).
        """
        entry = self._entries.get(alpaca_sym)
        if not entry:
            return False

        # --- Update running peak ---
        if price > entry["peak_price"]:
            entry["peak_price"] = price

        # --- Pyramiding: add to winner once it's up PYRAMID_TRIGGER_PCT ---
        if not entry.get("pyramided") and entry["orig_qty"] > 0:
            gain_pct = (price - entry["price"]) / entry["price"]
            if gain_pct >= PYRAMID_TRIGGER_PCT:
                add_qty = math.floor(
                    entry["orig_qty"] * PYRAMID_ADD_PCT * 1_000_000
                ) / 1_000_000
                add_cost = add_qty * price
                crypto_bp = self.trader.get_crypto_buying_power()
                if add_qty > 0 and add_cost < crypto_bp * 0.95:
                    logger.info(
                        f"[LIVE] *** PYRAMID {symbol} +{add_qty:.6f} units @ ${price:.4f} "
                        f"(up {gain_pct*100:.1f}% from entry ${entry['price']:.4f}) ***"
                    )
                    if self.trader.buy_crypto(symbol, add_qty):
                        entry["pyramided"] = True
                        # Move initial stop to original entry (breakeven on base position)
                        entry["stop"] = entry["price"]
                        self._save_entries()
                        log_trade("BUY", symbol, add_qty, price, 0,
                                  note=f"Pyramid add | base_entry=${entry['price']:.4f}")

        # --- Calculate trailing stop (trails TRAILING_STOP_PCT below peak) ---
        trailing_stop  = entry["peak_price"] * (1 - TRAILING_STOP_PCT)
        effective_stop = max(entry["stop"], trailing_stop)

        # Log when trail activates (first time it exceeds the initial floor)
        if trailing_stop > entry["stop"] and not entry.get("trail_active"):
            entry["trail_active"] = True
            gain_pct = (entry["peak_price"] - entry["price"]) / entry["price"] * 100
            logger.info(
                f"[LIVE] Trailing stop active: {symbol} peak=${entry['peak_price']:.4f} "
                f"(+{gain_pct:.1f}%) → trail stop=${trailing_stop:.4f}"
            )

        # --- Exit if price drops below effective stop ---
        if price <= effective_stop:
            pnl_pct = (price - entry["price"]) / entry["price"] * 100
            reason  = "Trail stop" if trailing_stop > entry["stop"] else "Initial stop"
            logger.info(
                f"[LIVE] *** {reason.upper()} HIT {symbol} @ ${price:.4f} "
                f"(stop=${effective_stop:.4f} | peak=${entry['peak_price']:.4f} | "
                f"pnl={pnl_pct:+.2f}%) ***"
            )
            if self.trader.sell_crypto(alpaca_sym):
                self._entries.pop(alpaca_sym, None)
                self._last_traded[alpaca_sym] = time.time()
                self._invalidate_pos_cache()
                log_trade("CLOSE", symbol, 0, price, 0,
                          note=f"{reason} | entry=${entry['price']:.4f} | "
                               f"peak=${entry['peak_price']:.4f} | pnl={pnl_pct:+.2f}%")

                # --- Update consecutive loss streak ---
                if pnl_pct > 0:
                    self._consecutive_losses = 0
                    self._loss_multiplier    = 1.0
                    logger.info(f"[KELLY] Win recorded — full position sizing restored")
                else:
                    self._consecutive_losses += 1
                    if self._consecutive_losses >= LOSS_THROTTLE_AFTER:
                        self._loss_multiplier = 0.5
                        logger.warning(
                            f"[KELLY] {self._consecutive_losses} consecutive losses — "
                            f"position size halved until next win"
                        )
                self._save_entries()
            return True

        return False

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

        # Manual stop / TP / breakeven check for open positions
        if holding:
            if self._check_exit_conditions(alpaca_sym, symbol, price):
                return  # Position was closed — skip buy/sell logic below

        # Cooldown — prevent signal-flip overtrading
        if time.time() - self._last_traded.get(alpaca_sym, 0) < TRADE_COOLDOWN:
            return

        # ---- BUY --------------------------------------------------------
        if signal["signal"] == "BUY" and not holding:

            # Daily loss circuit breaker
            if self._check_daily_halt():
                return

            # Learned blacklist — skip symbols that consistently lose
            if is_symbol_blacklisted(symbol):
                logger.info(f"[LIVE] Skip {symbol}: blacklisted by learner (poor win rate)")
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
            stop = calculate_stop_loss(price, atr)
            tp   = calculate_take_profit(price, atr)

            # Kelly-adjusted risk % (halved after consecutive losses)
            risk_pct = self._get_kelly_risk()
            qty = calculate_crypto_position_size(
                portfolio_value, price, stop,
                buying_power=crypto_bp * 0.98,
                risk_pct=risk_pct,
            )

            if qty <= 0:
                logger.info(f"[LIVE] Skip {symbol}: not enough cash (bp=${crypto_bp:.2f})")
                return

            logger.info(
                f"[LIVE] *** BUY  {symbol} | {qty:.6f} @ ${price:.4f} "
                f"| Stop: ${stop} | Score: {signal['score']} "
                f"| Risk: {risk_pct*100:.2f}% ***"
            )
            if self.trader.buy_crypto(symbol, qty, stop_loss=stop, take_profit=tp):
                self._last_traded[alpaca_sym] = time.time()
                self._entries[alpaca_sym]     = {
                    "price": price, "atr": atr,
                    "stop": stop,           # initial hard floor (entry - 2×ATR, max -4%)
                    "peak_price": price,    # tracks highest price seen since entry
                    "orig_qty": qty,        # used to size the pyramid add-on
                    "pyramided": False,     # True after pyramid add executed
                    "trail_active": False,  # flips True once trail exceeds initial stop
                }
                self._invalidate_pos_cache()
                self._save_entries()
                # Log reasons so the learner can trace which indicators fired
                reasons_str = " | ".join(signal.get("reasons", [])[:5])
                log_trade("BUY", symbol, qty, price, signal["score"], stop, tp,
                          note=f"{signal.get('regime', '')} | {reasons_str}")

        # ---- SELL -------------------------------------------------------
        elif signal["signal"] == "SELL" and holding:
            logger.info(f"[LIVE] *** SELL {symbol} @ ${price:.4f} | Score: {signal['score']} ***")
            if self.trader.sell_crypto(alpaca_sym):
                self._last_traded[alpaca_sym] = time.time()
                self._entries.pop(alpaca_sym, None)
                self._invalidate_pos_cache()
                self._save_entries()
                log_trade("SELL", symbol, 0, price, signal["score"], note="Signal exit")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        self._refresh_base_data()
        print_learned_state()  # Show learned weights in Railway logs at startup

        # Subscribe to ALL candidates — on_bar filters to _active_symbols only
        for sym in CRYPTO_CANDIDATES:
            self.stream.subscribe_bars(self.on_bar, sym)

        logger.info(f"[LIVE] Streaming 1-min bars for {len(CRYPTO_CANDIDATES)} candidates")
        logger.info(
            f"[LIVE] Cooldown: {TRADE_COOLDOWN}s | "
            f"Daily loss limit: {DAILY_LOSS_LIMIT_PCT*100:.0f}% | "
            f"Trailing stop: {TRAILING_STOP_PCT*100:.0f}% | "
            f"Pyramid trigger: +{PYRAMID_TRIGGER_PCT*100:.0f}% | "
            f"BTC filter: {BTC_CORRELATION_FILTER}"
        )

        # Retry with exponential backoff — Alpaca limits concurrent WebSocket
        # connections. After a redeploy the old connection may still be alive
        # for a few seconds, causing "connection limit exceeded".
        max_retries = 8
        for attempt in range(max_retries):
            try:
                self.stream.run()
                break  # clean exit — won't normally reach here
            except ValueError as e:
                if "connection limit" in str(e).lower() and attempt < max_retries - 1:
                    wait = min(2 ** attempt * 5, 60)  # 5s, 10s, 20s, 40s, 60s...
                    logger.warning(
                        f"[LIVE] WebSocket connection limit — old connection still alive. "
                        f"Retrying in {wait}s (attempt {attempt+1}/{max_retries})"
                    )
                    time.sleep(wait)
                    # Recreate the stream object to get a fresh connection
                    self.stream = CryptoDataStream(ALPACA_API_KEY, ALPACA_SECRET_KEY)
                    for sym in CRYPTO_CANDIDATES:
                        self.stream.subscribe_bars(self.on_bar, sym)
                else:
                    raise
