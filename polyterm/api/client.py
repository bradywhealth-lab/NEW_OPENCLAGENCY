"""Polymarket CLOB API client — async wrapper around py_clob_client."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType as ClobOrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from polyterm.api.models import (
    BookLevel,
    Market,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    PricePoint,
    Side,
    Trade,
)
from polyterm.config import Config

logger = logging.getLogger(__name__)

_SIDE_MAP = {Side.BUY: BUY, Side.SELL: SELL}
_ORDER_TYPE_MAP = {
    OrderType.GTC: ClobOrderType.GTC,
    OrderType.FOK: ClobOrderType.FOK,
}


class PolyClient:
    """Async-safe wrapper over the synchronous py_clob_client.ClobClient."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._clob: ClobClient | None = None
        self._creds: ApiCreds | None = None
        self._http = httpx.AsyncClient(
            base_url=config.gamma_host,
            timeout=15.0,
            headers={"Accept": "application/json"},
        )
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Lifecycle ───────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize CLOB client and derive API credentials."""
        if not self._config.has_credentials:
            logger.warning("No private key configured — read-only mode")
            self._clob = ClobClient(self._config.clob_host)
            self._connected = True
            return

        def _init() -> tuple[ClobClient, ApiCreds]:
            client = ClobClient(
                self._config.clob_host,
                key=self._config.private_key,
                chain_id=self._config.chain_id,
                signature_type=self._config.signature_type,
                funder=self._config.funder or None,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            return client, creds

        self._clob, self._creds = await asyncio.to_thread(_init)
        self._connected = True
        logger.info("Polymarket CLOB client connected")

    async def disconnect(self) -> None:
        await self._http.aclose()
        self._connected = False

    # ── Market Discovery (Gamma API) ───────────────────────────

    async def get_markets(self, limit: int = 50, active: bool = True) -> list[Market]:
        """Fetch markets from the Gamma API."""
        params: dict[str, Any] = {
            "limit": limit,
            "active": str(active).lower(),
            "closed": "false",
            "order": "volume",
            "ascending": "false",
        }
        resp = await self._http.get("/markets", params=params)
        resp.raise_for_status()
        raw = resp.json()

        markets: list[Market] = []
        for m in raw:
            tokens = m.get("clobTokenIds")
            if not tokens:
                continue
            prices_raw = m.get("outcomePrices", [])
            prices = []
            for p in prices_raw:
                try:
                    prices.append(float(p))
                except (ValueError, TypeError):
                    prices.append(0.0)

            markets.append(
                Market(
                    condition_id=m.get("conditionId", m.get("id", "")),
                    question=m.get("question", ""),
                    outcomes=m.get("outcomes", ["Yes", "No"]),
                    outcome_prices=prices,
                    token_ids=tokens if isinstance(tokens, list) else [tokens],
                    active=m.get("active", True),
                    volume=float(m.get("volume", 0) or 0),
                    end_date=m.get("endDate", ""),
                    image=m.get("image", ""),
                )
            )
        return markets

    # ── Order Book ──────────────────────────────────────────────

    async def get_order_book(self, token_id: str) -> OrderBook:
        """Fetch current L2 order book for a token."""
        if not self._clob:
            return OrderBook()

        def _fetch() -> dict:
            return self._clob.get_order_book(token_id)

        raw = await asyncio.to_thread(_fetch)
        return self._parse_book(raw)

    @staticmethod
    def _parse_book(raw: dict) -> OrderBook:
        bids = [
            BookLevel(price=float(b["price"]), size=float(b["size"]))
            for b in raw.get("bids", [])
        ]
        asks = [
            BookLevel(price=float(a["price"]), size=float(a["size"]))
            for a in raw.get("asks", [])
        ]
        bids.sort(key=lambda x: x.price, reverse=True)
        asks.sort(key=lambda x: x.price)
        return OrderBook(bids=bids, asks=asks)

    # ── Price History ───────────────────────────────────────────

    async def get_price_history(self, token_id: str) -> list[PricePoint]:
        """Fetch price history via CLOB REST endpoint."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._config.clob_host}/prices-history",
                    params={
                        "market": token_id,
                        "interval": "max",
                        "fidelity": 60,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()

            points: list[PricePoint] = []
            history = raw.get("history", raw) if isinstance(raw, dict) else raw
            if isinstance(history, list):
                for item in history:
                    if isinstance(item, dict):
                        points.append(
                            PricePoint(
                                timestamp=float(item.get("t", 0)),
                                price=float(item.get("p", 0)),
                            )
                        )
                    elif isinstance(item, (int, float)):
                        points.append(PricePoint(timestamp=0, price=float(item)))
            return points
        except Exception:
            logger.debug("Price history fetch failed", exc_info=True)
            return []

    # ── Order Execution ─────────────────────────────────────────

    async def place_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC,
    ) -> str:
        """Create, sign, and submit an order. Returns order ID."""
        if not self._clob or not self._creds:
            raise RuntimeError("Not authenticated — cannot place orders")

        clob = self._clob
        clob_side = _SIDE_MAP[side]
        clob_otype = _ORDER_TYPE_MAP.get(order_type, ClobOrderType.GTC)

        def _exec() -> dict:
            args = OrderArgs(
                token_id=token_id,
                side=clob_side,
                price=price,
                size=size,
            )
            signed = clob.create_order(args)
            return clob.post_order(signed, clob_otype)

        resp = await asyncio.to_thread(_exec)
        order_id = resp.get("orderID", resp.get("id", ""))
        logger.info("Order placed: %s %s %.2f @ %.4f → %s", side.value, token_id[:8], size, price, order_id)
        return order_id

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel a single order by ID."""
        if not self._clob:
            return False

        def _cancel() -> dict:
            return self._clob.cancel(order_id)

        try:
            await asyncio.to_thread(_cancel)
            logger.info("Order cancelled: %s", order_id)
            return True
        except Exception:
            logger.error("Failed to cancel order %s", order_id, exc_info=True)
            return False

    async def cancel_all(self) -> bool:
        """Cancel all open orders."""
        if not self._clob:
            return False

        def _cancel() -> dict:
            return self._clob.cancel_all()

        try:
            await asyncio.to_thread(_cancel)
            logger.info("All orders cancelled")
            return True
        except Exception:
            logger.error("Failed to cancel all orders", exc_info=True)
            return False

    # ── Open Orders ─────────────────────────────────────────────

    async def get_open_orders(self, market: str = "") -> list[Order]:
        """Fetch open orders, optionally filtered by market."""
        if not self._clob or not self._creds:
            return []

        def _fetch() -> list:
            params = OpenOrderParams(market=market) if market else OpenOrderParams()
            return self._clob.get_orders(params)

        try:
            raw = await asyncio.to_thread(_fetch)
            orders: list[Order] = []
            for o in raw if isinstance(raw, list) else []:
                orders.append(
                    Order(
                        order_id=o.get("id", ""),
                        token_id=o.get("asset_id", ""),
                        side=Side.BUY if o.get("side", "").upper() == "BUY" else Side.SELL,
                        price=float(o.get("price", 0)),
                        size=float(o.get("original_size", o.get("size", 0))),
                        size_matched=float(o.get("size_matched", 0)),
                        status=OrderStatus.LIVE,
                    )
                )
            return orders
        except Exception:
            logger.debug("Failed to fetch open orders", exc_info=True)
            return []

    # ── Positions ───────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """Fetch current positions from the data API."""
        if not self._clob or not self._creds:
            return []

        # py_clob_client doesn't expose positions directly — use data API
        try:
            # Derive address from clob client
            address = getattr(self._clob, "funder", "") or ""
            if not address:
                return []

            resp = await self._http.get(
                f"https://data-api.polymarket.com/positions",
                params={"user": address, "sizeThreshold": "0.01"},
            )
            resp.raise_for_status()
            raw = resp.json()
            positions: list[Position] = []
            for p in raw if isinstance(raw, list) else []:
                asset = p.get("asset", {}) if isinstance(p.get("asset"), dict) else {}
                positions.append(
                    Position(
                        token_id=asset.get("token_id", p.get("asset_id", "")),
                        size=float(p.get("size", 0)),
                        avg_price=float(p.get("avgPrice", 0)),
                        current_price=float(p.get("curPrice", p.get("price", 0))),
                        market_question=p.get("title", ""),
                        outcome=p.get("outcome", ""),
                    )
                )
            return positions
        except Exception:
            logger.debug("Failed to fetch positions", exc_info=True)
            return []

    # ── Utility ─────────────────────────────────────────────────

    async def get_midpoint(self, token_id: str) -> float:
        """Get current midpoint price for a token."""
        if not self._clob:
            return 0.0

        def _fetch() -> str:
            return self._clob.get_midpoint(token_id)

        try:
            mid = await asyncio.to_thread(_fetch)
            return float(mid)
        except Exception:
            return 0.0
