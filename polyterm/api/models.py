"""Data models for Polymarket CLOB entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    GTC = "GTC"
    GTD = "GTD"
    FOK = "FOK"
    IOC = "IOC"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    LIVE = "LIVE"
    MATCHED = "MATCHED"
    CANCELLED = "CANCELLED"


# ── Market ──────────────────────────────────────────────────────


@dataclass(slots=True)
class Market:
    condition_id: str
    question: str
    outcomes: list[str]
    outcome_prices: list[float]
    token_ids: list[str]
    active: bool = True
    volume: float = 0.0
    end_date: str = ""
    image: str = ""

    @property
    def slug(self) -> str:
        """Short display name: first 40 chars of question."""
        q = self.question
        return q[:40] + "..." if len(q) > 40 else q

    @property
    def yes_token(self) -> str:
        return self.token_ids[0] if self.token_ids else ""

    @property
    def no_token(self) -> str:
        return self.token_ids[1] if len(self.token_ids) > 1 else ""


# ── Order Book ──────────────────────────────────────────────────


@dataclass(slots=True)
class BookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderBook:
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    timestamp: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bids[0].price if self.bids else 0.0

    @property
    def best_ask(self) -> float:
        return self.asks[0].price if self.asks else 0.0

    @property
    def spread(self) -> float:
        if self.best_bid and self.best_ask:
            return round(self.best_ask - self.best_bid, 4)
        return 0.0

    @property
    def midpoint(self) -> float:
        if self.best_bid and self.best_ask:
            return round((self.best_bid + self.best_ask) / 2, 4)
        return 0.0


# ── Trade ───────────────────────────────────────────────────────


@dataclass(slots=True)
class Trade:
    price: float
    size: float
    side: Side
    timestamp: float
    trade_id: str = ""
    market: str = ""


# ── Price Point ─────────────────────────────────────────────────


@dataclass(slots=True)
class PricePoint:
    timestamp: float
    price: float


# ── Order ───────────────────────────────────────────────────────


@dataclass(slots=True)
class Order:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    size_matched: float = 0.0
    order_type: OrderType = OrderType.GTC
    status: OrderStatus = OrderStatus.LIVE
    market_question: str = ""

    @property
    def remaining(self) -> float:
        return self.size - self.size_matched


# ── Position ────────────────────────────────────────────────────


@dataclass(slots=True)
class Position:
    token_id: str
    size: float
    avg_price: float
    current_price: float = 0.0
    market_question: str = ""
    outcome: str = ""

    @property
    def market_value(self) -> float:
        return self.size * self.current_price

    @property
    def cost_basis(self) -> float:
        return self.size * self.avg_price

    @property
    def unrealized_pnl(self) -> float:
        return self.market_value - self.cost_basis

    @property
    def pnl_percent(self) -> float:
        if self.cost_basis == 0:
            return 0.0
        return (self.unrealized_pnl / self.cost_basis) * 100


# ── Price Quote ─────────────────────────────────────────────────


@dataclass(slots=True)
class PriceQuote:
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    mid: float = 0.0
