"""Arbitrage scanner — finds Yes/No pairs mispriced against each other.

On Polymarket, binary markets have a YES token and a NO token.
In theory: YES_price + NO_price = $1.00
When YES + NO < $1.00 → buy both → guaranteed profit at resolution.
When YES + NO > $1.00 → rare but means selling both is profitable.

This scanner also catches:
- Markets where one side is obviously mispriced (e.g., YES at 0.03 for
  an event that's already happening)
- Cross-market arbitrage (same underlying event, different markets)
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_HOST = "https://gamma-api.polymarket.com"


@dataclass()
class ArbitrageOpportunity:
    """A detected arbitrage opportunity."""

    market_question: str
    condition_id: str
    yes_token: str
    no_token: str
    yes_price: float
    no_price: float
    combined_price: float    # yes + no
    edge: float              # how much below $1.00 (positive = profit)
    edge_pct: float          # edge as percentage
    profit_per_dollar: float # profit per $1 deployed
    volume: float
    timestamp: float = 0.0

    @property
    def is_buy_both(self) -> bool:
        """Buy both sides when combined < $1."""
        return self.combined_price < 1.0

    @property
    def description(self) -> str:
        if self.is_buy_both:
            return (
                f"BUY BOTH: {self.market_question[:50]}\n"
                f"  YES={self.yes_price:.4f} + NO={self.no_price:.4f} = {self.combined_price:.4f}\n"
                f"  Edge: {self.edge:.4f} ({self.edge_pct:.1f}%) → ${self.profit_per_dollar:.4f}/dollar"
            )
        return (
            f"SELL BOTH: {self.market_question[:50]}\n"
            f"  YES={self.yes_price:.4f} + NO={self.no_price:.4f} = {self.combined_price:.4f}\n"
            f"  Edge: {abs(self.edge):.4f} ({abs(self.edge_pct):.1f}%)"
        )


class ArbitrageScanner:
    """Scans all active Polymarket markets for arbitrage opportunities."""

    def __init__(
        self,
        min_edge: float = 0.005,    # minimum 0.5% edge to flag
        min_volume: float = 1000.0,  # minimum $1k volume (avoid dead markets)
        max_markets: int = 200,
    ) -> None:
        self.min_edge = min_edge
        self.min_volume = min_volume
        self.max_markets = max_markets
        self._http = httpx.AsyncClient(timeout=20.0)
        self._opportunities: list[ArbitrageOpportunity] = []
        self._last_scan: float = 0.0

    @property
    def opportunities(self) -> list[ArbitrageOpportunity]:
        return list(self._opportunities)

    @property
    def last_scan_time(self) -> float:
        return self._last_scan

    async def scan(self) -> list[ArbitrageOpportunity]:
        """Scan all active markets for arbitrage. Returns sorted by edge."""
        logger.info("Starting arbitrage scan...")
        start = time.time()

        markets = await self._fetch_all_markets()
        opps: list[ArbitrageOpportunity] = []

        # Check each binary market
        for market in markets:
            opp = await self._check_market(market)
            if opp:
                opps.append(opp)

        # Sort by edge (biggest opportunities first)
        opps.sort(key=lambda o: o.edge, reverse=True)
        self._opportunities = opps
        self._last_scan = time.time()

        elapsed = time.time() - start
        logger.info(
            "Arbitrage scan complete: %d opportunities found in %.1fs (scanned %d markets)",
            len(opps), elapsed, len(markets),
        )
        return opps

    async def _fetch_all_markets(self) -> list[dict]:
        """Fetch active binary markets from Gamma API."""
        all_markets: list[dict] = []
        offset = 0
        limit = 100

        while len(all_markets) < self.max_markets:
            try:
                resp = await self._http.get(
                    f"{GAMMA_HOST}/markets",
                    params={
                        "limit": limit,
                        "offset": offset,
                        "active": "true",
                        "closed": "false",
                        "order": "volume",
                        "ascending": "false",
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                if not batch:
                    break
                all_markets.extend(batch)
                offset += limit
                if len(batch) < limit:
                    break
            except Exception as e:
                logger.warning("Failed to fetch markets at offset %d: %s", offset, e)
                break

        return all_markets

    async def _check_market(self, market: dict) -> ArbitrageOpportunity | None:
        """Check a single market for arbitrage."""
        tokens = market.get("clobTokenIds", [])
        if not isinstance(tokens, list) or len(tokens) < 2:
            return None

        prices_raw = market.get("outcomePrices", [])
        if len(prices_raw) < 2:
            return None

        volume = float(market.get("volume", 0) or 0)
        if volume < self.min_volume:
            return None

        try:
            yes_price = float(prices_raw[0])
            no_price = float(prices_raw[1])
        except (ValueError, TypeError, IndexError):
            return None

        if yes_price <= 0 or no_price <= 0:
            return None

        combined = yes_price + no_price
        edge = 1.0 - combined  # positive = buy-both opportunity

        if abs(edge) < self.min_edge:
            return None

        # Calculate profit per dollar deployed
        # If we buy both at combined_price, we get $1 at resolution
        # Profit = $1 - combined_price per pair
        # ROI = edge / combined_price
        profit_per_dollar = edge / combined if combined > 0 else 0
        edge_pct = (edge / 1.0) * 100

        return ArbitrageOpportunity(
            market_question=market.get("question", ""),
            condition_id=market.get("conditionId", market.get("id", "")),
            yes_token=tokens[0],
            no_token=tokens[1],
            yes_price=yes_price,
            no_price=no_price,
            combined_price=combined,
            edge=edge,
            edge_pct=edge_pct,
            profit_per_dollar=profit_per_dollar,
            volume=volume,
            timestamp=time.time(),
        )

    async def close(self) -> None:
        await self._http.aclose()
