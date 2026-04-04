"""Central market data store — aggregates all price/book/trade data."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from polyterm.api.models import (
    BookLevel,
    OrderBook,
    PricePoint,
    PriceQuote,
    Side,
    Trade,
)


class MarketDataStore:
    """Thread-safe central data store for all market data.

    Widgets poll this store on their refresh intervals.
    WebSocket callbacks write into this store.
    """

    def __init__(self, max_trades: int = 50, max_price_points: int = 200) -> None:
        self._max_trades = max_trades
        self._max_price_points = max_price_points
        self._lock = Lock()

        # State keyed by token_id
        self._books: dict[str, OrderBook] = {}
        self._trades: dict[str, list[Trade]] = defaultdict(list)
        self._price_history: dict[str, list[float]] = defaultdict(list)
        self._quotes: dict[str, PriceQuote] = {}
        self._last_update: dict[str, float] = {}

    # ── Writes (called from WebSocket callbacks) ────────────────

    def update_book(self, token_id: str, book: OrderBook) -> None:
        """Replace the order book for a token."""
        with self._lock:
            self._books[token_id] = book
            self._quotes[token_id] = PriceQuote(
                bid=book.best_bid,
                ask=book.best_ask,
                mid=book.midpoint,
                last=self._quotes.get(token_id, PriceQuote()).last,
            )
            self._last_update[token_id] = time.time()

    def update_book_from_raw(self, token_id: str, data: dict) -> None:
        """Parse raw WebSocket book event and update store."""
        bids = [
            BookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in data.get("bids", [])
            if b.get("price") and b.get("size")
        ]
        asks = [
            BookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in data.get("asks", [])
            if a.get("price") and a.get("size")
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        book = OrderBook(bids=bids, asks=asks, timestamp=time.time())
        self.update_book(token_id, book)

    def add_trade(self, token_id: str, trade: Trade) -> None:
        """Append a trade and update price history."""
        with self._lock:
            trades = self._trades[token_id]
            trades.insert(0, trade)
            if len(trades) > self._max_trades:
                trades[:] = trades[: self._max_trades]

            # Update last price
            quote = self._quotes.get(token_id, PriceQuote())
            self._quotes[token_id] = PriceQuote(
                bid=quote.bid, ask=quote.ask, mid=quote.mid, last=trade.price
            )

            # Append to price history
            history = self._price_history[token_id]
            history.append(trade.price)
            if len(history) > self._max_price_points:
                history[:] = history[-self._max_price_points :]

            self._last_update[token_id] = time.time()

    def add_trade_from_raw(self, token_id: str, data: dict) -> None:
        """Parse raw WebSocket trade event and add to store."""
        trade = Trade(
            price=float(data.get("price", 0)),
            size=float(data.get("size", 0)),
            side=Side.BUY if data.get("side", "").upper() == "BUY" else Side.SELL,
            timestamp=float(data.get("timestamp", time.time())),
            trade_id=data.get("id", ""),
        )
        if trade.price > 0:
            self.add_trade(token_id, trade)

    def set_price_history(self, token_id: str, prices: list[float]) -> None:
        """Bulk-set price history (from REST API initial load)."""
        with self._lock:
            self._price_history[token_id] = prices[-self._max_price_points :]

    # ── Reads (called from widgets) ─────────────────────────────

    def get_book(self, token_id: str) -> OrderBook:
        with self._lock:
            return self._books.get(token_id, OrderBook())

    def get_trades(self, token_id: str) -> list[Trade]:
        with self._lock:
            return list(self._trades.get(token_id, []))

    def get_quote(self, token_id: str) -> PriceQuote:
        with self._lock:
            return self._quotes.get(token_id, PriceQuote())

    def get_sparkline_data(self, token_id: str, width: int = 60) -> list[float]:
        """Return price history trimmed to fit chart width."""
        with self._lock:
            history = self._price_history.get(token_id, [])
            if len(history) <= width:
                return list(history)
            return list(history[-width:])

    def get_last_update(self, token_id: str) -> float:
        with self._lock:
            return self._last_update.get(token_id, 0.0)
