"""Position tracker — monitors open positions and calculates P&L."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from polyterm.api.client import PolyClient
from polyterm.api.models import Position, PriceQuote
from polyterm.core.market_data import MarketDataStore

logger = logging.getLogger(__name__)


@dataclass()
class PnLSummary:
    unrealized: float = 0.0
    realized: float = 0.0
    total: float = 0.0
    position_count: int = 0


class PositionTracker:
    """Tracks positions and computes live P&L using market data."""

    def __init__(self, client: PolyClient, data_store: MarketDataStore) -> None:
        self._client = client
        self._data_store = data_store
        self._positions: list[Position] = []
        self._realized_pnl: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def positions(self) -> list[Position]:
        return list(self._positions)

    async def refresh(self) -> list[Position]:
        """Fetch latest positions from the exchange."""
        try:
            positions = await self._client.get_positions()
            async with self._lock:
                self._positions = positions
            self._update_current_prices()
            return positions
        except Exception:
            logger.debug("Position refresh failed", exc_info=True)
            return self._positions

    def _update_current_prices(self) -> None:
        """Update position current prices from the data store."""
        for pos in self._positions:
            quote = self._data_store.get_quote(pos.token_id)
            if quote.mid > 0:
                pos.current_price = quote.mid
            elif quote.last > 0:
                pos.current_price = quote.last

    def get_pnl_summary(self) -> PnLSummary:
        """Calculate aggregate P&L across all positions."""
        self._update_current_prices()

        unrealized = sum(p.unrealized_pnl for p in self._positions)
        return PnLSummary(
            unrealized=round(unrealized, 4),
            realized=round(self._realized_pnl, 4),
            total=round(unrealized + self._realized_pnl, 4),
            position_count=len(self._positions),
        )

    def handle_fill(self, token_id: str, side: str, price: float, size: float) -> None:
        """Update positions based on a fill event."""
        for pos in self._positions:
            if pos.token_id == token_id:
                if side == "SELL" and pos.size > 0:
                    realized = (price - pos.avg_price) * min(size, pos.size)
                    self._realized_pnl += realized
                    pos.size -= size
                elif side == "BUY":
                    total_cost = pos.avg_price * pos.size + price * size
                    pos.size += size
                    if pos.size > 0:
                        pos.avg_price = total_cost / pos.size
                return
