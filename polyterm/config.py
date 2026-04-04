"""Configuration loader — reads .env and provides typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable application configuration."""

    # Credentials
    private_key: str = ""
    chain_id: int = 137
    signature_type: int = 0
    funder: str = ""

    # Endpoints
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    ws_market_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    ws_user_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # Refresh intervals (milliseconds)
    book_refresh_ms: int = 250
    chart_refresh_ms: int = 1000
    pnl_refresh_ms: int = 2000
    market_list_refresh_s: float = 30.0

    # Display
    book_depth: int = 10
    max_trades: int = 50
    chart_width: int = 60

    @property
    def has_credentials(self) -> bool:
        return bool(self.private_key and self.private_key != "0x...")

    @classmethod
    def load(cls, env_path: str | Path | None = None) -> Config:
        """Load config from environment / .env file."""
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        return cls(
            private_key=os.getenv("POLY_PRIVATE_KEY", ""),
            chain_id=int(os.getenv("POLY_CHAIN_ID", "137")),
            signature_type=int(os.getenv("POLY_SIGNATURE_TYPE", "0")),
            funder=os.getenv("POLY_FUNDER_ADDRESS", ""),
        )
