import logging

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce, PositionSide

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY, PAPER_TRADING

logger = logging.getLogger(__name__)


class AlpacaTrader:
    def __init__(self):
        if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
            raise ValueError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env file")

        self.client = TradingClient(
            ALPACA_API_KEY,
            ALPACA_SECRET_KEY,
            paper=PAPER_TRADING,
        )
        mode = "PAPER" if PAPER_TRADING else "LIVE"
        logger.info(f"AlpacaTrader initialized in {mode} mode")

    # --- Account ---

    def get_account(self):
        return self.client.get_account()

    def get_portfolio_value(self) -> float:
        return float(self.get_account().portfolio_value)

    def get_cash(self) -> float:
        return float(self.get_account().cash)

    def get_buying_power(self) -> float:
        """Buying power for marginable assets (ETFs). Accounts for margin,
        pending orders, and short position reserves."""
        return float(self.get_account().buying_power)

    def get_crypto_buying_power(self) -> float:
        """Available cash for crypto trading. Uses cash balance with a 0.5%
        safety buffer to absorb rounding differences at order time."""
        return float(self.get_account().cash) * 0.995

    def is_market_open(self) -> bool:
        return self.client.get_clock().is_open

    # --- Positions ---

    def get_positions(self) -> dict:
        """Returns {ticker: position_object} for all open positions."""
        return {p.symbol: p for p in self.client.get_all_positions()}

    def get_long_positions(self) -> dict:
        """Returns only long positions."""
        return {k: v for k, v in self.get_positions().items()
                if v.side == PositionSide.LONG}

    def get_short_positions(self) -> dict:
        """Returns only short positions."""
        return {k: v for k, v in self.get_positions().items()
                if v.side == PositionSide.SHORT}

    def get_position(self, ticker: str):
        try:
            return self.client.get_open_position(ticker)
        except Exception:
            return None

    # --- Orders ---

    def buy(self, ticker: str, qty: int,
            stop_loss: float = None, take_profit: float = None) -> bool:
        """Place a bracket buy order with stop-loss and take-profit enforced by Alpaca."""
        try:
            kwargs = dict(
                symbol=ticker,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            if stop_loss and take_profit:
                kwargs["order_class"]  = OrderClass.BRACKET
                kwargs["stop_loss"]    = StopLossRequest(stop_price=round(stop_loss, 2))
                kwargs["take_profit"]  = TakeProfitRequest(limit_price=round(take_profit, 2))

            result = self.client.submit_order(MarketOrderRequest(**kwargs))
            logger.info(f"BUY submitted: {qty}x {ticker} | SL=${stop_loss} TP=${take_profit} | ID: {result.id}")
            return True
        except Exception as e:
            logger.error(f"BUY failed for {ticker}: {e}")
            return False

    def sell(self, ticker: str, qty: int = None) -> bool:
        """
        Sell shares of a ticker.
        If qty is None, closes the entire position.
        """
        try:
            if qty is None:
                self.client.close_position(ticker)
                logger.info(f"Closed full position in {ticker}")
            else:
                order = MarketOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                self.client.submit_order(order)
                logger.info(f"SELL submitted: {qty}x {ticker}")
            return True
        except Exception as e:
            logger.error(f"SELL failed for {ticker}: {e}")
            return False

    def short(self, ticker: str, qty: int,
              stop_loss: float = None, take_profit: float = None) -> bool:
        """Open a short bracket order — stop is ABOVE entry, target is BELOW entry."""
        try:
            kwargs = dict(
                symbol=ticker,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            if stop_loss and take_profit:
                kwargs["order_class"]  = OrderClass.BRACKET
                kwargs["stop_loss"]    = StopLossRequest(stop_price=round(stop_loss, 2))
                kwargs["take_profit"]  = TakeProfitRequest(limit_price=round(take_profit, 2))

            result = self.client.submit_order(MarketOrderRequest(**kwargs))
            logger.info(f"SHORT submitted: {qty}x {ticker} | SL=${stop_loss} TP=${take_profit} | ID: {result.id}")
            return True
        except Exception as e:
            logger.error(f"SHORT failed for {ticker}: {e}")
            return False

    def cover(self, ticker: str) -> bool:
        """Close a short position (buy to cover)."""
        try:
            self.client.close_position(ticker)
            logger.info(f"COVERED short position in {ticker}")
            return True
        except Exception as e:
            logger.error(f"COVER failed for {ticker}: {e}")
            return False

    def buy_crypto(self, symbol: str, qty: float,
                   stop_loss: float = None, take_profit: float = None) -> bool:
        """Place a fractional bracket buy order for crypto (GTC, 24/7)."""
        try:
            kwargs = dict(
                symbol=symbol,
                qty=round(qty, 6),
                side=OrderSide.BUY,
                time_in_force=TimeInForce.GTC,
            )
            if stop_loss and take_profit:
                kwargs["order_class"]  = OrderClass.BRACKET
                kwargs["stop_loss"]    = StopLossRequest(stop_price=round(stop_loss, 4))
                kwargs["take_profit"]  = TakeProfitRequest(limit_price=round(take_profit, 4))

            result = self.client.submit_order(MarketOrderRequest(**kwargs))
            logger.info(f"CRYPTO BUY submitted: {qty:.6f}x {symbol} | SL=${stop_loss} TP=${take_profit} | ID: {result.id}")
            return True
        except Exception as e:
            logger.error(f"CRYPTO BUY failed for {symbol}: {e}")
            return False

    def sell_crypto(self, symbol: str) -> bool:
        """Close entire crypto position."""
        try:
            self.client.close_position(symbol)
            logger.info(f"CRYPTO SELL: Closed full position in {symbol}")
            return True
        except Exception as e:
            logger.error(f"CRYPTO SELL failed for {symbol}: {e}")
            return False

    def close_all_positions(self) -> None:
        """Emergency: close everything."""
        logger.warning("Closing ALL positions!")
        self.client.close_all_positions(cancel_orders=True)
