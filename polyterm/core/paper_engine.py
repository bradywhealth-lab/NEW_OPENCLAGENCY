"""Paper trading engine — simulates order execution against live market data."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

from polyterm.api.models import (
    BookLevel,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Trade,
)
from polyterm.core.market_data import MarketDataStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PaperFill:
    """Record of a simulated fill."""

    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    timestamp: float


class PaperTradingEngine:
    """Simulates order matching against live order book data.

    - Limit orders fill when the book crosses the order price
    - Market orders fill at the current best bid/ask
    - Tracks simulated positions, P&L, and order history
    """

    def __init__(self, data_store: MarketDataStore, starting_balance: float = 10_000.0) -> None:
        self._store = data_store
        self._balance = starting_balance
        self._starting_balance = starting_balance
        self._open_orders: list[Order] = []
        self._fills: list[PaperFill] = []
        self._positions: dict[str, Position] = {}  # token_id -> Position
        self._realized_pnl: float = 0.0

    @property
    def balance(self) -> float:
        return self._balance

    @property
    def open_orders(self) -> list[Order]:
        return list(self._open_orders)

    @property
    def positions(self) -> list[Position]:
        return list(self._positions.values())

    @property
    def fills(self) -> list[PaperFill]:
        return list(self._fills)

    @property
    def realized_pnl(self) -> float:
        return self._realized_pnl

    # ── Order Submission ────────────────────────────────────────

    def place_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC,
    ) -> tuple[bool, str]:
        """Place a paper order. Returns (success, order_id_or_error)."""
        cost = price * size
        if side == Side.BUY and cost > self._balance:
            return False, f"Insufficient balance: need ${cost:.2f}, have ${self._balance:.2f}"

        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        order = Order(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            order_type=order_type,
            status=OrderStatus.LIVE,
        )

        # Market orders: fill immediately at best available price
        book = self._store.get_book(token_id)
        if order_type == OrderType.FOK:
            return self._try_market_fill(order, book)

        # Limit orders: check if immediately fillable, otherwise queue
        filled = self._try_limit_fill(order, book)
        if not filled:
            self._open_orders.append(order)
            logger.info("[PAPER] Limit order queued: %s %s %.1f @ %.4f",
                        side.value, token_id[:8], size, price)

        return True, order_id

    def cancel_order(self, order_id: str) -> bool:
        """Cancel a paper order."""
        before = len(self._open_orders)
        self._open_orders = [o for o in self._open_orders if o.order_id != order_id]
        cancelled = len(self._open_orders) < before
        if cancelled:
            logger.info("[PAPER] Order cancelled: %s", order_id)
        return cancelled

    def cancel_all(self) -> int:
        """Cancel all paper orders."""
        count = len(self._open_orders)
        self._open_orders.clear()
        logger.info("[PAPER] Cancelled %d orders", count)
        return count

    # ── Fill Logic ──────────────────────────────────────────────

    def _try_market_fill(self, order: Order, book: OrderBook) -> tuple[bool, str]:
        """Fill a market order at best available price."""
        if order.side == Side.BUY:
            if not book.asks:
                return False, "No asks available"
            fill_price = book.asks[0].price
        else:
            if not book.bids:
                return False, "No bids available"
            fill_price = book.bids[0].price

        self._execute_fill(order, fill_price, order.size)
        return True, order.order_id

    def _try_limit_fill(self, order: Order, book: OrderBook) -> bool:
        """Check if a limit order can fill immediately."""
        if order.side == Side.BUY:
            # Buy limit fills if best ask <= order price
            if book.asks and book.asks[0].price <= order.price:
                self._execute_fill(order, book.asks[0].price, order.size)
                return True
        else:
            # Sell limit fills if best bid >= order price
            if book.bids and book.bids[0].price >= order.price:
                self._execute_fill(order, book.bids[0].price, order.size)
                return True
        return False

    def _execute_fill(self, order: Order, fill_price: float, fill_size: float) -> None:
        """Execute a fill and update positions/balance."""
        fill = PaperFill(
            order_id=order.order_id,
            token_id=order.token_id,
            side=order.side,
            price=fill_price,
            size=fill_size,
            timestamp=time.time(),
        )
        self._fills.append(fill)

        if order.side == Side.BUY:
            cost = fill_price * fill_size
            self._balance -= cost
            self._update_position_buy(order.token_id, fill_price, fill_size)
        else:
            proceeds = fill_price * fill_size
            self._balance += proceeds
            self._update_position_sell(order.token_id, fill_price, fill_size)

        order.status = OrderStatus.MATCHED
        order.size_matched = fill_size

        logger.info(
            "[PAPER] FILL: %s %s %.1f @ %.4f | Balance: $%.2f",
            order.side.value, order.token_id[:8], fill_size, fill_price, self._balance,
        )

    def _update_position_buy(self, token_id: str, price: float, size: float) -> None:
        """Update position after a buy fill."""
        if token_id in self._positions:
            pos = self._positions[token_id]
            total_cost = pos.avg_price * pos.size + price * size
            pos.size += size
            if pos.size > 0:
                pos.avg_price = total_cost / pos.size
        else:
            self._positions[token_id] = Position(
                token_id=token_id,
                size=size,
                avg_price=price,
                current_price=price,
                outcome="Yes",
            )

    def _update_position_sell(self, token_id: str, price: float, size: float) -> None:
        """Update position after a sell fill."""
        if token_id in self._positions:
            pos = self._positions[token_id]
            realized = (price - pos.avg_price) * min(size, pos.size)
            self._realized_pnl += realized
            pos.size -= size
            if pos.size <= 0.001:
                del self._positions[token_id]
        else:
            # Short selling: create negative position
            self._positions[token_id] = Position(
                token_id=token_id,
                size=-size,
                avg_price=price,
                current_price=price,
                outcome="Yes",
            )

    # ── Periodic Check ──────────────────────────────────────────

    def check_pending_orders(self) -> list[PaperFill]:
        """Check if any pending limit orders should fill against current book.

        Call this periodically (e.g., every 250ms alongside book refresh).
        """
        new_fills: list[PaperFill] = []
        still_open: list[Order] = []

        for order in self._open_orders:
            book = self._store.get_book(order.token_id)
            filled = self._try_limit_fill(order, book)
            if filled:
                new_fills.append(self._fills[-1])
            else:
                still_open.append(order)

        self._open_orders = still_open
        return new_fills

    def update_mark_prices(self) -> None:
        """Update unrealized P&L for all positions."""
        for token_id, pos in self._positions.items():
            quote = self._store.get_quote(token_id)
            if quote.mid > 0:
                pos.current_price = quote.mid
            elif quote.last > 0:
                pos.current_price = quote.last
