"""Crypto price edge scanner — compares Polymarket crypto markets to real prices.

Polymarket has markets like "Will BTC be above $70k on April 30?"
If BTC is currently at $72k with 3 weeks left, and the market says 55% YES,
that's likely mispriced — real probability is much higher.

This scanner:
1. Fetches real-time crypto prices from CoinGecko (free, no API key)
2. Finds Polymarket crypto price threshold markets
3. Compares current price to threshold to estimate true probability
4. Flags markets where Polymarket price diverges from estimate
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass

import httpx

from polyterm.api.gamma_utils import parse_outcome_prices, parse_token_ids

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
COINGECKO_API = "https://api.coingecko.com/api/v3"

# Map common names to CoinGecko IDs
CRYPTO_MAP = {
    "btc": "bitcoin",
    "bitcoin": "bitcoin",
    "eth": "ethereum",
    "ethereum": "ethereum",
    "sol": "solana",
    "solana": "solana",
    "doge": "dogecoin",
    "xrp": "ripple",
    "ada": "cardano",
    "matic": "matic-network",
    "avax": "avalanche-2",
    "link": "chainlink",
    "dot": "polkadot",
    "bnb": "binancecoin",
}

# Regex to extract price thresholds from market questions
PRICE_PATTERNS = [
    re.compile(r"(?:above|over|exceed|higher than|≥|>=?)\s*\$?([\d,]+(?:\.\d+)?[kmb]?)", re.I),
    re.compile(r"(?:below|under|lower than|≤|<=?)\s*\$?([\d,]+(?:\.\d+)?[kmb]?)", re.I),
    re.compile(r"(?:reach|hit|touch)\s*\$?([\d,]+(?:\.\d+)?[kmb]?)", re.I),
    re.compile(r"\$([\d,]+(?:\.\d+)?[kmb]?)\b", re.I),  # plain $100k, $1m
    re.compile(r"\$?([\d,]+(?:\.\d+)?[kmb]?)\s*(?:or more|or higher|or above|\+)", re.I),
]


@dataclass()
class CryptoEdge:
    """A detected crypto price mispricing."""

    market_question: str
    condition_id: str
    token_id: str
    crypto: str
    current_price: float
    threshold_price: float
    market_yes_price: float   # what Polymarket thinks
    estimated_prob: float     # what we think
    edge: float               # estimated_prob - market_yes_price
    edge_pct: float
    side: str                 # "BUY_YES" or "BUY_NO"
    confidence: str           # "HIGH", "MEDIUM", "LOW"
    volume: float
    timestamp: float = 0.0

    @property
    def description(self) -> str:
        return (
            f"{self.side}: {self.market_question[:50]}\n"
            f"  {self.crypto.upper()} now=${self.current_price:,.0f} vs threshold=${self.threshold_price:,.0f}\n"
            f"  Market={self.market_yes_price:.0%} vs Estimate={self.estimated_prob:.0%}\n"
            f"  Edge: {self.edge_pct:+.1f}% [{self.confidence}]"
        )


class CryptoEdgeScanner:
    """Scans Polymarket crypto markets for mispricing vs real prices."""

    def __init__(
        self,
        min_edge: float = 0.08,      # 8% minimum edge to flag
        min_volume: float = 5000.0,
        volatility_annual: float = 0.65,  # BTC ~65% annual vol
    ) -> None:
        self.min_edge = min_edge
        self.min_volume = min_volume
        self.volatility = volatility_annual
        self._http = httpx.AsyncClient(timeout=15.0)
        self._price_cache: dict[str, tuple[float, float]] = {}  # coin -> (price, timestamp)
        self._edges: list[CryptoEdge] = []

    @property
    def edges(self) -> list[CryptoEdge]:
        return list(self._edges)

    async def scan(self) -> list[CryptoEdge]:
        """Scan crypto markets for mispricing opportunities."""
        logger.info("Starting crypto edge scan...")

        # Fetch current crypto prices
        await self._refresh_prices()

        # Fetch crypto-related markets
        markets = await self._fetch_crypto_markets()
        edges: list[CryptoEdge] = []

        for market in markets:
            edge = self._analyze_market(market)
            if edge:
                edges.append(edge)

        edges.sort(key=lambda e: abs(e.edge), reverse=True)
        self._edges = edges

        logger.info("Crypto scan: %d edges found from %d markets", len(edges), len(markets))
        return edges

    async def _refresh_prices(self) -> None:
        """Fetch latest crypto prices from CoinGecko."""
        coin_ids = ",".join(set(CRYPTO_MAP.values()))
        try:
            resp = await self._http.get(
                f"{COINGECKO_API}/simple/price",
                params={"ids": coin_ids, "vs_currencies": "usd"},
            )
            resp.raise_for_status()
            data = resp.json()
            now = time.time()
            for coin_id, prices in data.items():
                self._price_cache[coin_id] = (prices["usd"], now)
            logger.info("Fetched prices for %d coins", len(data))
        except Exception as e:
            logger.warning("Failed to fetch crypto prices: %s", e)

    async def _fetch_crypto_markets(self) -> list[dict]:
        """Fetch Polymarket markets related to crypto prices."""
        all_markets: list[dict] = []
        keywords = ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto", "price"]

        for keyword in keywords[:3]:  # Limit API calls
            try:
                resp = await self._http.get(
                    f"{GAMMA_HOST}/markets",
                    params={
                        "limit": 50,
                        "active": "true",
                        "closed": "false",
                        "tag": "crypto",
                    },
                )
                resp.raise_for_status()
                batch = resp.json()
                all_markets.extend(batch)
                break  # tag search usually gets them all
            except Exception:
                pass

        # Deduplicate by condition_id
        seen = set()
        unique = []
        for m in all_markets:
            cid = m.get("conditionId", m.get("id", ""))
            if cid not in seen:
                seen.add(cid)
                unique.append(m)

        return unique

    def _analyze_market(self, market: dict) -> CryptoEdge | None:
        """Analyze a single crypto market for mispricing."""
        question = market.get("question", "")
        tokens = parse_token_ids(market)
        prices = parse_outcome_prices(market)
        volume = float(market.get("volume", 0) or 0)

        if volume < self.min_volume or len(tokens) < 2 or len(prices) < 2:
            return None

        yes_price = prices[0]

        # Detect which crypto this market is about
        crypto = self._detect_crypto(question)
        if not crypto:
            return None

        # Get current price
        coin_id = CRYPTO_MAP.get(crypto)
        if not coin_id or coin_id not in self._price_cache:
            return None
        current_price, _ = self._price_cache[coin_id]

        # Extract price threshold from question
        threshold = self._extract_threshold(question)
        if not threshold or threshold <= 0:
            return None

        # Estimate true probability using simplified model
        # Based on: how far is current price from threshold + time remaining
        end_date = market.get("endDate", "")
        days_left = self._estimate_days_left(end_date)
        if days_left <= 0:
            days_left = 1

        estimated_prob = self._estimate_probability(
            current_price, threshold, days_left, question
        )

        edge = estimated_prob - yes_price

        if abs(edge) < self.min_edge:
            return None

        # Determine trade direction
        if edge > 0:
            side = "BUY_YES"
        else:
            side = "BUY_NO"

        # Confidence level
        if abs(edge) > 0.20:
            confidence = "HIGH"
        elif abs(edge) > 0.12:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return CryptoEdge(
            market_question=question,
            condition_id=market.get("conditionId", market.get("id", "")),
            token_id=tokens[0] if edge > 0 else tokens[1],
            crypto=crypto,
            current_price=current_price,
            threshold_price=threshold,
            market_yes_price=yes_price,
            estimated_prob=estimated_prob,
            edge=edge,
            edge_pct=edge * 100,
            side=side,
            confidence=confidence,
            volume=volume,
            timestamp=time.time(),
        )

    def _detect_crypto(self, question: str) -> str | None:
        """Detect which cryptocurrency a market question is about."""
        q_lower = question.lower()
        for name, _ in CRYPTO_MAP.items():
            # Word boundary match to avoid false positives
            if re.search(r'\b' + re.escape(name) + r'\b', q_lower):
                return name
        return None

    def _extract_threshold(self, question: str) -> float | None:
        """Extract a price threshold from a market question."""
        for pattern in PRICE_PATTERNS:
            match = pattern.search(question)
            if match:
                val_str = match.group(1).replace(",", "")
                suffix = val_str[-1].lower() if val_str else ""
                if suffix == "k":
                    return float(val_str[:-1]) * 1_000
                elif suffix == "m":
                    return float(val_str[:-1]) * 1_000_000
                elif suffix == "b":
                    return float(val_str[:-1]) * 1_000_000_000
                return float(val_str)
        return None

    def _estimate_days_left(self, end_date: str) -> float:
        """Estimate days until market resolution."""
        if not end_date:
            return 30  # default assumption
        try:
            from datetime import datetime
            end = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            now = datetime.now(end.tzinfo) if end.tzinfo else datetime.now()
            delta = (end - now).total_seconds() / 86400
            return max(delta, 0.1)
        except Exception:
            return 30

    def _estimate_probability(
        self,
        current: float,
        threshold: float,
        days_left: float,
        question: str,
    ) -> float:
        """Estimate probability using log-normal model.

        P(S_T > K) = N(d2) where:
        d2 = [ln(S/K) + (r - σ²/2)T] / (σ√T)

        Simplified: we use 0 drift (r=0) for crypto.
        """
        q_lower = question.lower()
        is_above = any(w in q_lower for w in ["above", "over", "exceed", "higher", "reach", "hit"])
        is_below = any(w in q_lower for w in ["below", "under", "lower"])

        if current <= 0 or threshold <= 0:
            return 0.5

        T = days_left / 365.0
        sigma = self.volatility

        try:
            d2 = (math.log(current / threshold) - 0.5 * sigma ** 2 * T) / (sigma * math.sqrt(T))
            prob_above = self._normal_cdf(d2)
        except (ValueError, ZeroDivisionError):
            prob_above = 0.5

        if is_below:
            return 1.0 - prob_above
        return prob_above

    @staticmethod
    def _normal_cdf(x: float) -> float:
        """Approximate standard normal CDF."""
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))

    async def close(self) -> None:
        await self._http.aclose()
