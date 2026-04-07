"""Configuration loader — reads .env and provides typed settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    """Immutable application configuration."""

    # Credentials
    private_key: str = ""
    chain_id: int = 137
    signature_type: int = 0
    funder: str = ""

    # Trading mode
    paper_trading: bool = False
    paper_balance: float = 10_000.0

    # Copy trading
    copy_target: str = ""          # Target wallet address to copy
    copy_enabled: bool = False
    copy_multiplier: float = 1.0   # Size multiplier for copied trades
    copy_max_size: float = 500.0   # Max USDC per copied position
    copy_auto_execute: bool = False
    copy_poll_interval: float = 5.0

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

    @property
    def trading_mode(self) -> str:
        """Return current trading mode string."""
        if self.paper_trading:
            return "PAPER"
        if self.has_credentials:
            return "LIVE"
        return "READ-ONLY"

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
            paper_trading=os.getenv("POLY_PAPER_TRADING", "").lower() in ("1", "true", "yes"),
            paper_balance=float(os.getenv("POLY_PAPER_BALANCE", "10000")),
            copy_target=os.getenv("POLY_COPY_TARGET", ""),
            copy_enabled=os.getenv("POLY_COPY_ENABLED", "").lower() in ("1", "true", "yes"),
            copy_multiplier=float(os.getenv("POLY_COPY_MULTIPLIER", "1.0")),
            copy_max_size=float(os.getenv("POLY_COPY_MAX_SIZE", "500")),
            copy_auto_execute=os.getenv("POLY_COPY_AUTO_EXECUTE", "").lower() in ("1", "true", "yes"),
            copy_poll_interval=float(os.getenv("POLY_COPY_POLL_INTERVAL", "5")),
        )
