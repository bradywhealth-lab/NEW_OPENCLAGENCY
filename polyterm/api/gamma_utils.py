"""Gamma API response parsing utilities.

The Gamma API returns some fields (clobTokenIds, outcomePrices, outcomes)
as JSON-encoded strings rather than native lists. This module handles
both formats transparently.
"""

from __future__ import annotations

import json
from typing import Any


def parse_json_field(value: Any, default: Any = None) -> Any:
    """Parse a field that may be a JSON-encoded string or already decoded."""
    if value is None:
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (list, dict)):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    return default if default is not None else []


def parse_token_ids(market: dict) -> list[str]:
    """Extract token IDs from a Gamma API market response."""
    raw = market.get("clobTokenIds")
    tokens = parse_json_field(raw, [])
    if isinstance(tokens, list) and len(tokens) >= 1:
        return [str(t) for t in tokens]
    return []


def parse_outcome_prices(market: dict) -> list[float]:
    """Extract outcome prices from a Gamma API market response."""
    raw = market.get("outcomePrices", [])
    items = parse_json_field(raw, [])
    prices = []
    for p in items:
        try:
            prices.append(float(p))
        except (ValueError, TypeError):
            prices.append(0.0)
    return prices
