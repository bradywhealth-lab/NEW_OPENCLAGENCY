"""Order manager — place, cancel, and track orders."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from textual.message import Message

from polyterm.api.client import PolyClient
from polyterm.api.models import Order, OrderStatus, OrderType, Side

logger = logging.getLogger(__name__)


class OrderEvent(Message):
    """Fired when order state changes — widgets listen for this."""

    def __init__(self, action: str, order_id: str, detail: str = "") -> None:
        super().__init__()
        self.action = action  # "placed", "cancelled", "filled", "error"
        self.order_id = order_id
        self.detail = detail


class OrderManager:
    """Manages order lifecycle — submit, cancel, track."""

    def __init__(self, client: PolyClient) -> None:
        self._client = client
        self._open_orders: list[Order] = []
        self._lock = asyncio.Lock()

    @property
    def open_orders(self) -> list[Order]:
        return list(self._open_orders)

    async def submit_order(
        self,
        token_id: str,
        side: Side,
        price: float,
        size: float,
        order_type: OrderType = OrderType.GTC,
    ) -> tuple[bool, str]:
        """Submit an order. Returns (success, order_id_or_error)."""
        try:
            order_id = await self._client.place_order(
                token_id=token_id,
                side=side,
                price=price,
                size=size,
                order_type=order_type,
            )
            # Add to local tracking
            async with self._lock:
                self._open_orders.append(
                    Order(
                        order_id=order_id,
                        token_id=token_id,
                        side=side,
                        price=price,
                        size=size,
                        order_type=order_type,
                        status=OrderStatus.LIVE,
                    )
                )
            logger.info("Order submitted: %s", order_id)
            return True, order_id

        except Exception as e:
            logger.error("Order submission failed: %s", e)
            return False, str(e)

    async def cancel(self, order_id: str) -> bool:
        """Cancel a single order."""
        success = await self._client.cancel_order(order_id)
        if success:
            async with self._lock:
                self._open_orders = [
                    o for o in self._open_orders if o.order_id != order_id
                ]
        return success

    async def cancel_all(self) -> int:
        """Cancel all open orders. Returns count cancelled."""
        success = await self._client.cancel_all()
        if success:
            count = len(self._open_orders)
            async with self._lock:
                self._open_orders.clear()
            return count
        return 0

    async def refresh(self) -> list[Order]:
        """Sync open orders from the exchange."""
        orders = await self._client.get_open_orders()
        async with self._lock:
            self._open_orders = orders
        return orders

    def handle_user_event(self, data: dict) -> OrderEvent | None:
        """Process a user WebSocket event for order updates."""
        event_type = data.get("type", data.get("event_type", ""))
        order_id = data.get("order_id", data.get("id", ""))

        if event_type in ("order_matched", "trade"):
            # Remove fully filled orders
            matched = float(data.get("matched_amount", 0))
            for o in self._open_orders:
                if o.order_id == order_id:
                    o.size_matched += matched
                    if o.remaining <= 0:
                        o.status = OrderStatus.MATCHED
                        self._open_orders = [
                            x for x in self._open_orders if x.order_id != order_id
                        ]
                    break
            return OrderEvent("filled", order_id, f"matched {matched}")

        elif event_type == "order_cancelled":
            self._open_orders = [
                o for o in self._open_orders if o.order_id != order_id
            ]
            return OrderEvent("cancelled", order_id)

        return None
