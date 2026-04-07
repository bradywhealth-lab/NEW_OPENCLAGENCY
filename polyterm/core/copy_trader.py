"""Copy trade module — monitors a target wallet and mirrors trades."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Any

import httpx

from polyterm.api.models import Side, OrderType
from polyterm.config import Config

logger = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass(slots=True)
class CopyTradeEvent:
    """A trade detected on the target wallet that should be mirrored."""

    token_id: str
    side: Side
    price: float
    size: float
    market_question: str
    target_address: str
    timestamp: float
    condition_id: str = ""


@dataclass(slots=True)
class CopyTradeConfig:
    """Configuration for copy trading behavior."""

    target_address: str              # Wallet address to copy
    enabled: bool = True
    size_multiplier: float = 1.0     # Scale factor for position sizes
    max_position_size: float = 500.0 # Max USDC per position
    copy_buys: bool = True
    copy_sells: bool = True
    auto_execute: bool = False       # If True, execute immediately without confirmation
    poll_interval_s: float = 5.0     # How often to check target's activity
    slippage_tolerance: float = 0.02 # Max price difference to accept (2%)


OnCopyTrade = Callable[[CopyTradeEvent], Coroutine[Any, Any, None]]


class CopyTrader:
    """Monitors a target wallet's Polymarket activity and generates copy signals.

    Polls the Polymarket Data API for the target's recent trades.
    When a new trade is detected, calls the `on_trade` callback so the
    app can either auto-execute or prompt the user.
    """

    def __init__(
        self,
        config: CopyTradeConfig,
        on_trade: OnCopyTrade | None = None,
    ) -> None:
        self.config = config
        self._on_trade = on_trade
        self._seen_trades: set[str] = set()  # trade IDs already processed
        self._last_poll: float = 0.0
        self._running = False
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(timeout=15.0)
        self._market_cache: dict[str, str] = {}  # condition_id -> question

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Start the copy trade polling loop."""
        if self._running:
            return
        self._running = True

        # Seed seen trades so we don't copy historical positions
        await self._seed_existing_trades()

        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "Copy trader started — watching %s (x%.1f, interval %.0fs)",
            self.config.target_address[:10],
            self.config.size_multiplier,
            self.config.poll_interval_s,
        )

    async def stop(self) -> None:
        """Stop the copy trade polling loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._http.aclose()
        logger.info("Copy trader stopped")

    # ── Polling Loop ────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Main poll loop — checks target wallet for new activity."""
        while self._running:
            try:
                await self._check_target_activity()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Copy trade poll error: %s", e)

            await asyncio.sleep(self.config.poll_interval_s)

    async def _seed_existing_trades(self) -> None:
        """Load existing trades so we don't replay history."""
        try:
            trades = await self._fetch_target_trades(limit=100)
            for t in trades:
                trade_id = t.get("id", t.get("transactionHash", ""))
                if trade_id:
                    self._seen_trades.add(trade_id)
            logger.info("Seeded %d existing trades for target", len(self._seen_trades))
        except Exception as e:
            logger.warning("Failed to seed existing trades: %s", e)

    async def _check_target_activity(self) -> None:
        """Fetch recent trades from the target wallet, emit new ones."""
        trades = await self._fetch_target_trades(limit=20)

        for t in trades:
            trade_id = t.get("id", t.get("transactionHash", ""))
            if not trade_id or trade_id in self._seen_trades:
                continue

            self._seen_trades.add(trade_id)

            # Parse trade data
            side_str = t.get("side", "").upper()
            if side_str == "BUY" and not self.config.copy_buys:
                continue
            if side_str == "SELL" and not self.config.copy_sells:
                continue

            token_id = t.get("asset_id", t.get("tokenId", ""))
            raw_price = float(t.get("price", 0))
            raw_size = float(t.get("size", t.get("amount", 0)))
            condition_id = t.get("market", t.get("conditionId", ""))

            if raw_price <= 0 or raw_size <= 0:
                continue

            # Scale the size
            scaled_size = raw_size * self.config.size_multiplier
            max_shares = self.config.max_position_size / raw_price if raw_price > 0 else 0
            scaled_size = min(scaled_size, max_shares)

            if scaled_size <= 0:
                continue

            # Resolve market question
            question = await self._resolve_market_question(condition_id)

            event = CopyTradeEvent(
                token_id=token_id,
                side=Side.BUY if side_str == "BUY" else Side.SELL,
                price=raw_price,
                size=round(scaled_size, 1),
                market_question=question,
                target_address=self.config.target_address,
                timestamp=float(t.get("timestamp", time.time())),
                condition_id=condition_id,
            )

            logger.info(
                "[COPY] Detected: %s %s %.1f @ %.4f — %s",
                event.side.value, token_id[:8], event.size, event.price, question[:40],
            )

            if self._on_trade:
                await self._on_trade(event)

    # ── Data Fetching ───────────────────────────────────────────

    async def _fetch_target_trades(self, limit: int = 20) -> list[dict]:
        """Fetch recent trades for the target address."""
        try:
            resp = await self._http.get(
                f"{DATA_API}/activity",
                params={
                    "user": self.config.target_address,
                    "limit": limit,
                    "offset": 0,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else data.get("data", data.get("history", []))
        except httpx.HTTPStatusError:
            # Fallback: try /trades endpoint
            try:
                resp = await self._http.get(
                    f"{DATA_API}/trades",
                    params={
                        "user": self.config.target_address,
                        "limit": limit,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data if isinstance(data, list) else []
            except Exception:
                return []
        except Exception:
            return []

    async def _resolve_market_question(self, condition_id: str) -> str:
        """Look up the human-readable market question."""
        if not condition_id:
            return "Unknown market"
        if condition_id in self._market_cache:
            return self._market_cache[condition_id]

        try:
            resp = await self._http.get(
                f"{GAMMA_API}/markets",
                params={"id": condition_id, "limit": 1},
            )
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list) and data:
                question = data[0].get("question", "Unknown")
                self._market_cache[condition_id] = question
                return question
        except Exception:
            pass

        self._market_cache[condition_id] = f"Market {condition_id[:12]}..."
        return self._market_cache[condition_id]

    # ── Query Methods ───────────────────────────────────────────

    async def get_target_positions(self) -> list[dict]:
        """Fetch the target wallet's current positions."""
        try:
            resp = await self._http.get(
                f"{DATA_API}/positions",
                params={
                    "user": self.config.target_address,
                    "sizeThreshold": "0.01",
                    "sort": "CURRENT",
                    "order": "DESC",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    async def get_target_pnl(self) -> dict:
        """Fetch the target wallet's total value / P&L summary."""
        try:
            resp = await self._http.get(
                f"{DATA_API}/total_value",
                params={"user": self.config.target_address},
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}
