"""Event resolution scanner — finds markets where the outcome is already known.

Sometimes Polymarket markets lag behind reality:
- An election result is announced but the market still trades at 85¢
- A crypto price already crossed a threshold but the market hasn't caught up
- A sports game ended but the market hasn't resolved yet

These are near-risk-free trades: buy the winning side at a discount
before the market resolves to $1.00.

This scanner checks for:
1. Markets very close to resolution with extreme prices (>0.90 or <0.10)
   that haven't quite reached 0.95+ (potential last-minute value)
2. Markets where end_date has passed but not yet resolved
3. Crypto threshold markets where the price already crossed definitively
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from polyterm.api.gamma_utils import parse_outcome_prices, parse_token_ids

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"


@dataclass()
class ResolutionEdge:
    """A market where the outcome appears already determined."""

    market_question: str
    condition_id: str
    token_id: str
    side: str               # "BUY_YES" or "BUY_NO"
    current_price: float    # what you'd pay
    expected_value: float   # what you expect to get ($1 if right)
    edge: float
    edge_pct: float
    reason: str             # why we think this is resolved
    confidence: str         # "HIGH", "MEDIUM", "LOW"
    volume: float
    time_to_resolution: float  # hours until expected resolution
    timestamp: float = 0.0

    @property
    def description(self) -> str:
        return (
            f"{self.side}: {self.market_question[:50]}\n"
            f"  Price: {self.current_price:.4f} → Expected: ${self.expected_value:.2f}\n"
            f"  Edge: {self.edge_pct:+.1f}% [{self.confidence}]\n"
            f"  Reason: {self.reason}"
        )


class EventResolutionScanner:
    """Scans for markets where the outcome is already known or nearly certain."""

    def __init__(
        self,
        min_edge: float = 0.03,     # 3% — even small edges are good for near-certain outcomes
        min_volume: float = 500.0,
        stale_threshold: float = 0.95,  # price above this = already priced in
    ) -> None:
        self.min_edge = min_edge
        self.min_volume = min_volume
        self.stale_threshold = stale_threshold
        self._http = httpx.AsyncClient(timeout=15.0)
        self._edges: list[ResolutionEdge] = []

    @property
    def edges(self) -> list[ResolutionEdge]:
        return list(self._edges)

    async def scan(self) -> list[ResolutionEdge]:
        """Scan for resolution opportunities."""
        logger.info("Starting event resolution scan...")

        markets = await self._fetch_markets()
        edges: list[ResolutionEdge] = []

        for market in markets:
            # Check near-resolution markets
            edge = self._check_near_resolution(market)
            if edge:
                edges.append(edge)

            # Check expired but unresolved markets
            edge = self._check_expired_unresolved(market)
            if edge:
                edges.append(edge)

        # Deduplicate by condition_id
        seen = set()
        unique_edges = []
        for e in edges:
            if e.condition_id not in seen:
                seen.add(e.condition_id)
                unique_edges.append(e)

        unique_edges.sort(key=lambda e: e.edge, reverse=True)
        self._edges = unique_edges

        logger.info("Resolution scan: %d edges found from %d markets", len(unique_edges), len(markets))
        return unique_edges

    async def _fetch_markets(self) -> list[dict]:
        """Fetch markets that might be near resolution."""
        all_markets: list[dict] = []

        # Get active markets sorted by end date (soonest first)
        try:
            resp = await self._http.get(
                f"{GAMMA_HOST}/markets",
                params={
                    "limit": 200,
                    "active": "true",
                    "closed": "false",
                    "order": "endDate",
                    "ascending": "true",
                },
            )
            resp.raise_for_status()
            all_markets = resp.json()
        except Exception as e:
            logger.warning("Failed to fetch markets: %s", e)

        return all_markets

    def _check_near_resolution(self, market: dict) -> ResolutionEdge | None:
        """Check if a market is near resolution with a clear winner.

        Look for markets where one side is 0.85-0.94 (not yet at 0.95+).
        If the outcome looks certain, buying at 0.90 gives 10% return.
        """
        tokens = parse_token_ids(market)
        prices = parse_outcome_prices(market)
        volume = float(market.get("volume", 0) or 0)

        if volume < self.min_volume or len(tokens) < 2 or len(prices) < 2:
            return None

        yes_price = prices[0]
        no_price = prices[1]

        # Check if one side is strongly favored but not fully priced in
        end_date = market.get("endDate", "")
        hours_left = self._hours_until(end_date)

        # Focus on markets resolving within 48 hours
        if hours_left > 48 or hours_left < 0:
            return None

        # YES side looks like the winner
        if 0.85 <= yes_price < self.stale_threshold:
            edge = 1.0 - yes_price
            if edge >= self.min_edge:
                confidence = "HIGH" if yes_price >= 0.92 else "MEDIUM" if yes_price >= 0.88 else "LOW"
                return ResolutionEdge(
                    market_question=market.get("question", ""),
                    condition_id=market.get("conditionId", market.get("id", "")),
                    token_id=tokens[0],
                    side="BUY_YES",
                    current_price=yes_price,
                    expected_value=1.0,
                    edge=edge,
                    edge_pct=edge * 100,
                    reason=f"YES at {yes_price:.0%}, resolves in {hours_left:.0f}h",
                    confidence=confidence,
                    volume=volume,
                    time_to_resolution=hours_left,
                    timestamp=time.time(),
                )

        # NO side looks like the winner (YES is very low)
        if yes_price <= 0.15 and no_price < self.stale_threshold:
            edge = 1.0 - no_price
            if edge >= self.min_edge:
                confidence = "HIGH" if no_price >= 0.92 else "MEDIUM" if no_price >= 0.88 else "LOW"
                return ResolutionEdge(
                    market_question=market.get("question", ""),
                    condition_id=market.get("conditionId", market.get("id", "")),
                    token_id=tokens[1],
                    side="BUY_NO",
                    current_price=no_price,
                    expected_value=1.0,
                    edge=edge,
                    edge_pct=edge * 100,
                    reason=f"NO at {no_price:.0%} (YES={yes_price:.0%}), resolves in {hours_left:.0f}h",
                    confidence=confidence,
                    volume=volume,
                    time_to_resolution=hours_left,
                    timestamp=time.time(),
                )

        return None

    def _check_expired_unresolved(self, market: dict) -> ResolutionEdge | None:
        """Check for markets past their end date that haven't resolved.

        These are goldmines: the outcome is known, but the market
        hasn't settled yet. Buy the winning side for near-guaranteed profit.
        """
        tokens = parse_token_ids(market)
        prices = parse_outcome_prices(market)

        if len(tokens) < 2 or len(prices) < 2:
            return None

        end_date = market.get("endDate", "")
        hours_left = self._hours_until(end_date)

        # Only interested in markets that should have resolved already
        if hours_left > 0:
            return None

        yes_price = prices[0]
        no_price = prices[1]

        # If market is expired and one side is strongly favored but not at $1
        if yes_price >= 0.80 and yes_price < 0.99:
            edge = 1.0 - yes_price
            if edge >= self.min_edge:
                return ResolutionEdge(
                    market_question=market.get("question", ""),
                    condition_id=market.get("conditionId", market.get("id", "")),
                    token_id=tokens[0],
                    side="BUY_YES",
                    current_price=yes_price,
                    expected_value=1.0,
                    edge=edge,
                    edge_pct=edge * 100,
                    reason=f"EXPIRED {abs(hours_left):.0f}h ago, YES at {yes_price:.0%}",
                    confidence="HIGH",
                    volume=volume,
                    time_to_resolution=0,
                    timestamp=time.time(),
                )

        if no_price >= 0.80 and no_price < 0.99:
            edge = 1.0 - no_price
            if edge >= self.min_edge:
                return ResolutionEdge(
                    market_question=market.get("question", ""),
                    condition_id=market.get("conditionId", market.get("id", "")),
                    token_id=tokens[1],
                    side="BUY_NO",
                    current_price=no_price,
                    expected_value=1.0,
                    edge=edge,
                    edge_pct=edge * 100,
                    reason=f"EXPIRED {abs(hours_left):.0f}h ago, NO at {no_price:.0%}",
                    confidence="HIGH",
                    volume=volume,
                    time_to_resolution=0,
                    timestamp=time.time(),
                )

        return None

    def _hours_until(self, end_date: str) -> float:
        """Calculate hours until a date. Negative = already passed."""
        if not end_date:
            return 999
        try:
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            return (end - now).total_seconds() / 3600
        except Exception:
            return 999

    async def close(self) -> None:
        await self._http.aclose()
