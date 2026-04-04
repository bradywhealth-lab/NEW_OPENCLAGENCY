"""WebSocket manager — concurrent market + user channel streams."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine

import websockets
from websockets.asyncio.client import ClientConnection

from polyterm.config import Config

logger = logging.getLogger(__name__)

Callback = Callable[[dict[str, Any]], Coroutine[Any, Any, None]]

PING_INTERVAL = 9  # seconds (server expects < 10s)
RECONNECT_DELAYS = [1, 2, 4, 8, 15, 30]  # exponential backoff


class WSManager:
    """Manages persistent WebSocket connections to Polymarket CLOB."""

    def __init__(
        self,
        config: Config,
        on_book_update: Callback | None = None,
        on_trade: Callback | None = None,
        on_user_event: Callback | None = None,
    ) -> None:
        self._config = config
        self._on_book_update = on_book_update
        self._on_trade = on_trade
        self._on_user_event = on_user_event

        self._market_ws: ClientConnection | None = None
        self._user_ws: ClientConnection | None = None
        self._market_task: asyncio.Task | None = None
        self._user_task: asyncio.Task | None = None
        self._subscribed_assets: set[str] = set()
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self._running and self._market_ws is not None

    # ── Public API ──────────────────────────────────────────────

    async def start(self, asset_ids: list[str] | None = None) -> None:
        """Start WebSocket connections."""
        self._running = True
        if asset_ids:
            self._subscribed_assets = set(asset_ids)
        self._market_task = asyncio.create_task(
            self._run_with_reconnect("market", self._run_market_socket)
        )

    async def start_user_channel(self, api_creds: dict) -> None:
        """Start user channel for order/position updates."""
        self._user_task = asyncio.create_task(
            self._run_with_reconnect(
                "user", lambda: self._run_user_socket(api_creds)
            )
        )

    async def subscribe(self, asset_ids: list[str]) -> None:
        """Subscribe to additional market assets."""
        new_ids = set(asset_ids) - self._subscribed_assets
        if not new_ids:
            return
        self._subscribed_assets.update(new_ids)
        if self._market_ws:
            msg = json.dumps({
                "assets_ids": list(new_ids),
                "type": "subscribe",
            })
            try:
                await self._market_ws.send(msg)
                logger.debug("Subscribed to %d new assets", len(new_ids))
            except Exception:
                logger.warning("Failed to send subscribe message")

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        """Unsubscribe from market assets."""
        self._subscribed_assets -= set(asset_ids)

    async def stop(self) -> None:
        """Gracefully shutdown all WebSocket connections."""
        self._running = False
        for task in (self._market_task, self._user_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        for ws in (self._market_ws, self._user_ws):
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
        self._market_ws = None
        self._user_ws = None
        logger.info("WebSocket manager stopped")

    # ── Internal Loops ──────────────────────────────────────────

    async def _run_with_reconnect(self, name: str, connect_fn: Callable) -> None:
        """Reconnect loop with exponential backoff."""
        attempt = 0
        while self._running:
            try:
                await connect_fn()
            except asyncio.CancelledError:
                return
            except Exception as e:
                if not self._running:
                    return
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                logger.warning(
                    "WS %s disconnected (%s), reconnecting in %ds...",
                    name, e, delay,
                )
                await asyncio.sleep(delay)
                attempt += 1
            else:
                attempt = 0  # reset on clean disconnect

    async def _run_market_socket(self) -> None:
        """Connect to market channel and stream updates."""
        url = self._config.ws_market_url
        logger.info("Connecting to market WebSocket: %s", url)

        async with websockets.connect(url, ping_interval=None) as ws:
            self._market_ws = ws

            # Subscribe to tracked assets
            if self._subscribed_assets:
                sub_msg = json.dumps({
                    "assets_ids": list(self._subscribed_assets),
                    "type": "subscribe",
                })
                await ws.send(sub_msg)
                logger.info("Subscribed to %d assets", len(self._subscribed_assets))

            last_ping = time.monotonic()

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    # Send keepalive ping
                    await ws.ping()
                    last_ping = time.monotonic()
                    continue

                # Send periodic pings
                if time.monotonic() - last_ping > PING_INTERVAL:
                    await ws.ping()
                    last_ping = time.monotonic()

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                await self._dispatch_market_event(data)

        self._market_ws = None

    async def _run_user_socket(self, api_creds: dict) -> None:
        """Connect to user channel for personal order/trade updates."""
        url = self._config.ws_user_url
        logger.info("Connecting to user WebSocket: %s", url)

        async with websockets.connect(url, ping_interval=None) as ws:
            self._user_ws = ws

            auth_msg = json.dumps({
                "type": "subscribe",
                "auth": api_creds,
            })
            await ws.send(auth_msg)

            last_ping = time.monotonic()

            while self._running:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    await ws.ping()
                    last_ping = time.monotonic()
                    continue

                if time.monotonic() - last_ping > PING_INTERVAL:
                    await ws.ping()
                    last_ping = time.monotonic()

                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                if self._on_user_event:
                    await self._on_user_event(data)

        self._user_ws = None

    # ── Event Dispatch ──────────────────────────────────────────

    async def _dispatch_market_event(self, data: dict) -> None:
        """Route incoming market WebSocket messages to callbacks."""
        # Handle array of events
        events = data if isinstance(data, list) else [data]

        for event in events:
            event_type = event.get("event_type", event.get("type", ""))

            if event_type in ("book", "price_change"):
                if self._on_book_update:
                    await self._on_book_update(event)

            elif event_type in ("trade", "last_trade_price"):
                if self._on_trade:
                    await self._on_trade(event)

            elif event_type == "tick_size_change":
                logger.debug("Tick size change: %s", event)

            else:
                logger.debug("Unhandled market event: %s", event_type)
