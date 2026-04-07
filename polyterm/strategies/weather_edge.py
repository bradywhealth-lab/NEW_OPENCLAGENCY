"""Weather edge scanner — compares Polymarket weather markets to real forecasts.

Polymarket has markets like "Will NYC temperature exceed 80°F on Friday?"
We fetch the actual NOAA/NWS forecast and compare to the market price.

If the forecast says 90% chance of exceeding 80°F but the market is at 60¢,
that's a 30% edge.

Uses the free National Weather Service (NWS) API — no API key needed.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass

import httpx

from polyterm.api.gamma_utils import parse_outcome_prices, parse_token_ids

logger = logging.getLogger(__name__)

GAMMA_HOST = "https://gamma-api.polymarket.com"
NWS_API = "https://api.weather.gov"
# OpenMeteo free API for international cities (no key needed)
OPENMETEO_API = "https://api.open-meteo.com/v1/forecast"

# Major city coordinates — US cities use NWS, international use OpenMeteo
CITY_COORDS = {
    # US cities (NWS)
    "new york": (40.7128, -74.0060),
    "nyc": (40.7128, -74.0060),
    "los angeles": (34.0522, -118.2437),
    "chicago": (41.8781, -87.6298),
    "miami": (25.7617, -80.1918),
    "houston": (29.7604, -95.3698),
    "phoenix": (33.4484, -112.0740),
    "dallas": (32.7767, -96.7970),
    "denver": (39.7392, -104.9903),
    "seattle": (47.6062, -122.3321),
    "boston": (42.3601, -71.0589),
    "atlanta": (33.7490, -84.3880),
    "san francisco": (37.7749, -122.4194),
    "washington": (38.9072, -77.0369),
    # International cities (OpenMeteo)
    "london": (51.5074, -0.1278),
    "paris": (48.8566, 2.3522),
    "tokyo": (35.6762, 139.6503),
    "sydney": (-33.8688, 151.2093),
    "toronto": (43.6532, -79.3832),
    "dubai": (25.2048, 55.2708),
    "mumbai": (19.0760, 72.8777),
    "singapore": (1.3521, 103.8198),
    "riyadh": (24.7136, 46.6753),
}

# US cities that can use NWS API
US_CITIES = {
    "new york", "nyc", "los angeles", "chicago", "miami", "houston",
    "phoenix", "dallas", "denver", "seattle", "boston", "atlanta",
    "san francisco", "washington",
}

# Temperature extraction patterns — require explicit degree/unit markers
TEMP_PATTERNS = [
    re.compile(r"(\d+)\s*°\s*[fFcC]", re.I),         # 80°F, 16°C
    re.compile(r"(\d+)\s*degrees?\s*[fFcC]", re.I),   # 80 degrees F
    re.compile(r"(?:above|over|exceed|higher than|≥)\s*(\d+)\s*°", re.I),
    re.compile(r"(?:below|under|lower than|≤)\s*(\d+)\s*°", re.I),
]


@dataclass()
class WeatherEdge:
    """A detected weather market mispricing."""

    market_question: str
    condition_id: str
    token_id: str
    city: str
    forecast_high: float | None
    forecast_low: float | None
    threshold_temp: float
    market_yes_price: float
    estimated_prob: float
    edge: float
    edge_pct: float
    side: str               # "BUY_YES" or "BUY_NO"
    confidence: str
    forecast_source: str
    volume: float
    timestamp: float = 0.0

    @property
    def description(self) -> str:
        hi = f"H:{self.forecast_high:.0f}°F" if self.forecast_high else "?"
        lo = f"L:{self.forecast_low:.0f}°F" if self.forecast_low else "?"
        return (
            f"{self.side}: {self.market_question[:50]}\n"
            f"  Forecast: {hi} {lo} vs threshold {self.threshold_temp:.0f}°F ({self.city})\n"
            f"  Market={self.market_yes_price:.0%} vs Estimate={self.estimated_prob:.0%}\n"
            f"  Edge: {self.edge_pct:+.1f}% [{self.confidence}] (src: {self.forecast_source})"
        )


class WeatherEdgeScanner:
    """Scans Polymarket weather markets and compares to NWS forecasts."""

    def __init__(
        self,
        min_edge: float = 0.10,     # 10% minimum edge
        min_volume: float = 1000.0,
    ) -> None:
        self.min_edge = min_edge
        self.min_volume = min_volume
        self._http = httpx.AsyncClient(
            timeout=15.0,
            headers={"User-Agent": "PolyTerm/1.0 (weather@polyterm.app)"},
        )
        self._nws_grid_cache: dict[str, dict] = {}
        self._edges: list[WeatherEdge] = []

    @property
    def edges(self) -> list[WeatherEdge]:
        return list(self._edges)

    async def scan(self) -> list[WeatherEdge]:
        """Scan weather markets for mispricing."""
        logger.info("Starting weather edge scan...")

        markets = await self._fetch_weather_markets()
        edges: list[WeatherEdge] = []

        for market in markets:
            edge = await self._analyze_market(market)
            if edge:
                edges.append(edge)

        edges.sort(key=lambda e: abs(e.edge), reverse=True)
        self._edges = edges

        logger.info("Weather scan: %d edges found from %d markets", len(edges), len(markets))
        return edges

    async def _fetch_weather_markets(self) -> list[dict]:
        """Fetch weather-related markets from Polymarket."""
        try:
            resp = await self._http.get(
                f"{GAMMA_HOST}/markets",
                params={
                    "limit": 100,
                    "active": "true",
                    "closed": "false",
                    "tag": "weather",
                },
            )
            resp.raise_for_status()
            markets = resp.json()

            # Also search by keywords if tag search doesn't work
            if not markets:
                resp = await self._http.get(
                    f"{GAMMA_HOST}/markets",
                    params={
                        "limit": 100,
                        "active": "true",
                        "closed": "false",
                    },
                )
                resp.raise_for_status()
                all_markets = resp.json()
                weather_keywords = ["temperature", "weather", "°f", "°c", "rain", "snow", "wind", "hurricane"]
                markets = [
                    m for m in all_markets
                    if any(kw in m.get("question", "").lower() for kw in weather_keywords)
                ]

            return markets
        except Exception as e:
            logger.warning("Failed to fetch weather markets: %s", e)
            return []

    async def _analyze_market(self, market: dict) -> WeatherEdge | None:
        """Analyze a weather market against real forecast data."""
        question = market.get("question", "")
        tokens = parse_token_ids(market)
        prices = parse_outcome_prices(market)
        volume = float(market.get("volume", 0) or 0)

        if volume < self.min_volume or len(tokens) < 2 or len(prices) < 2:
            return None

        yes_price = prices[0]

        # Detect city
        city = self._detect_city(question)
        if not city:
            return None

        # Extract temperature threshold
        threshold = self._extract_temperature(question)
        if not threshold:
            return None

        # Convert °C to °F if the question uses Celsius (forecasts are in °F)
        is_celsius = "°c" in question.lower() or "celsius" in question.lower()
        threshold_f = threshold * 9.0 / 5.0 + 32 if is_celsius else threshold

        # Fetch actual forecast (returns °F)
        forecast = await self._get_forecast(city)
        if not forecast:
            return None

        # Estimate probability
        is_above = any(w in question.lower() for w in ["above", "over", "exceed", "higher", "high"])
        estimated_prob = self._estimate_temp_probability(
            forecast_high=forecast.get("high"),
            forecast_low=forecast.get("low"),
            threshold=threshold_f,
            is_above=is_above,
        )

        if estimated_prob is None:
            return None

        edge = estimated_prob - yes_price
        if abs(edge) < self.min_edge:
            return None

        side = "BUY_YES" if edge > 0 else "BUY_NO"

        if abs(edge) > 0.25:
            confidence = "HIGH"
        elif abs(edge) > 0.15:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return WeatherEdge(
            market_question=question,
            condition_id=market.get("conditionId", market.get("id", "")),
            token_id=tokens[0] if edge > 0 else tokens[1],
            city=city,
            forecast_high=forecast.get("high"),
            forecast_low=forecast.get("low"),
            threshold_temp=threshold,
            market_yes_price=yes_price,
            estimated_prob=estimated_prob,
            edge=edge,
            edge_pct=edge * 100,
            side=side,
            confidence=confidence,
            forecast_source="NWS",
            volume=volume,
            timestamp=time.time(),
        )

    def _detect_city(self, question: str) -> str | None:
        """Detect which city a weather question refers to."""
        q_lower = question.lower()
        for city_name in CITY_COORDS:
            # Use word boundary matching to avoid "la" matching "zealand"
            pattern = r'\b' + re.escape(city_name) + r'\b'
            if re.search(pattern, q_lower):
                return city_name
        return None

    def _extract_temperature(self, question: str) -> float | None:
        """Extract a temperature threshold from a question."""
        for pattern in TEMP_PATTERNS:
            match = pattern.search(question)
            if match:
                return float(match.group(1))
        return None

    async def _get_forecast(self, city: str) -> dict | None:
        """Get forecast for a city. Uses NWS for US, OpenMeteo for international."""
        coords = CITY_COORDS.get(city)
        if not coords:
            return None

        if city in US_CITIES:
            return await self._get_nws_forecast(city, coords)
        else:
            return await self._get_openmeteo_forecast(city, coords)

    async def _get_nws_forecast(self, city: str, coords: tuple) -> dict | None:
        """Get NWS forecast for a US city. Returns {high, low}."""
        cache_key = f"{coords[0]},{coords[1]}"
        if cache_key not in self._nws_grid_cache:
            try:
                resp = await self._http.get(
                    f"{NWS_API}/points/{coords[0]},{coords[1]}"
                )
                resp.raise_for_status()
                data = resp.json()
                self._nws_grid_cache[cache_key] = data["properties"]
            except Exception as e:
                logger.debug("NWS grid lookup failed for %s: %s", city, e)
                return None

        grid = self._nws_grid_cache[cache_key]
        forecast_url = grid.get("forecast")
        if not forecast_url:
            return None

        try:
            resp = await self._http.get(forecast_url)
            resp.raise_for_status()
            data = resp.json()
            periods = data.get("properties", {}).get("periods", [])

            if not periods:
                return None

            high = None
            low = None
            for period in periods[:4]:
                temp = period.get("temperature")
                is_day = period.get("isDaytime", True)
                if temp is not None:
                    if is_day and (high is None or temp > high):
                        high = float(temp)
                    elif not is_day and (low is None or temp < low):
                        low = float(temp)

            if high is not None or low is not None:
                return {"high": high, "low": low}

        except Exception as e:
            logger.debug("NWS forecast fetch failed for %s: %s", city, e)

        return None

    async def _get_openmeteo_forecast(self, city: str, coords: tuple) -> dict | None:
        """Get OpenMeteo forecast for international cities. Returns {high, low} in °F."""
        try:
            resp = await self._http.get(
                OPENMETEO_API,
                params={
                    "latitude": coords[0],
                    "longitude": coords[1],
                    "daily": "temperature_2m_max,temperature_2m_min",
                    "temperature_unit": "fahrenheit",
                    "forecast_days": 3,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily", {})
            highs = daily.get("temperature_2m_max", [])
            lows = daily.get("temperature_2m_min", [])

            if highs and lows:
                return {"high": float(highs[0]), "low": float(lows[0])}
        except Exception as e:
            logger.debug("OpenMeteo forecast failed for %s: %s", city, e)

        return None

    def _estimate_temp_probability(
        self,
        forecast_high: float | None,
        forecast_low: float | None,
        threshold: float,
        is_above: bool,
    ) -> float | None:
        """Estimate probability of exceeding/going below threshold.

        NWS forecasts have ~3-5°F uncertainty. We model it as a
        normal distribution centered on the forecast with σ=3°F.
        """
        import math

        sigma = 3.0  # NWS forecast uncertainty in °F

        if is_above and forecast_high is not None:
            # P(actual_high > threshold)
            z = (forecast_high - threshold) / sigma
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))
        elif not is_above and forecast_low is not None:
            # P(actual_low < threshold)
            z = (threshold - forecast_low) / sigma
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))

        return None

    async def close(self) -> None:
        await self._http.aclose()
