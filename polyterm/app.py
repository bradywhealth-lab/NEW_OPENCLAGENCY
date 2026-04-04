"""PolyTerm — Lightweight Polymarket CLOB Trading Terminal.

Single-screen TUI with real-time price feeds, L2 order book,
ASCII charts, live P&L, and instant order execution.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding

from polyterm.api.client import PolyClient
from polyterm.api.models import Market, Side
from polyterm.api.websocket import WSManager
from polyterm.config import Config
from polyterm.core.market_data import MarketDataStore
from polyterm.core.order_manager import OrderManager
from polyterm.core.position_tracker import PositionTracker
from polyterm.widgets.market_list import MarketList, MarketSelected
from polyterm.widgets.order_book import OrderBookWidget
from polyterm.widgets.order_entry import CancelAllRequested, OrderEntryPanel, OrderSubmitted
from polyterm.widgets.positions import PositionsTable
from polyterm.widgets.price_chart import PriceChartWidget
from polyterm.widgets.status_bar import StatusBarWidget
from polyterm.widgets.trade_tape import TradeTape

logger = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "styles" / "terminal.tcss"


class PolyTermApp(App):
    """Main trading terminal application."""

    TITLE = "PolyTerm"
    SUB_TITLE = "Polymarket CLOB Terminal"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("q", "quit", "Quit", priority=True),
        Binding("b", "focus_buy", "Buy", show=False),
        Binding("s", "focus_sell", "Sell", show=False),
        Binding("r", "refresh_positions", "Refresh", show=False),
        Binding("escape", "clear_form", "Clear", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._config = Config.load()
        self._client = PolyClient(self._config)
        self._data_store = MarketDataStore(
            max_trades=self._config.max_trades,
        )
        self._order_mgr = OrderManager(self._client)
        self._pos_tracker = PositionTracker(self._client, self._data_store)
        self._ws_mgr = WSManager(
            self._config,
            on_book_update=self._handle_book_update,
            on_trade=self._handle_trade,
            on_user_event=self._handle_user_event,
        )
        self._markets: list[Market] = []
        self._active_token: str = ""
        self._active_market: Market | None = None

    # ── Layout ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        # Row 1: Status bar (spans 3 columns)
        yield StatusBarWidget()
        # Row 2: Market list | Price chart | Order entry
        yield MarketList()
        yield PriceChartWidget()
        yield OrderEntryPanel()
        # Row 3: Order book | Trade tape | Positions
        yield OrderBookWidget(depth=self._config.book_depth)
        yield TradeTape()
        yield PositionsTable()

    # ── Lifecycle ───────────────────────────────────────────────

    async def on_mount(self) -> None:
        """Initialize connections and start data feeds."""
        self._connect_api()
        self._load_markets()

        # Start periodic UI refresh timers
        self.set_interval(
            self._config.book_refresh_ms / 1000, self._refresh_book_ui
        )
        self.set_interval(
            self._config.chart_refresh_ms / 1000, self._refresh_chart_ui
        )
        self.set_interval(
            self._config.pnl_refresh_ms / 1000, self._refresh_positions_ui
        )
        self.set_interval(1.0, self._refresh_status_bar)
        self.set_interval(0.5, self._refresh_trades_ui)

    async def on_unmount(self) -> None:
        """Clean shutdown."""
        await self._ws_mgr.stop()
        await self._client.disconnect()

    # ── API Connection (background workers) ─────────────────────

    @work(thread=False)
    async def _connect_api(self) -> None:
        """Connect to Polymarket API."""
        try:
            await self._client.connect()
            status = self.query_one(StatusBarWidget)
            status.connected = True
            logger.info("API connected")
        except Exception as e:
            logger.error("API connection failed: %s", e)
            status = self.query_one(StatusBarWidget)
            status.connected = False

    @work(thread=False)
    async def _load_markets(self) -> None:
        """Fetch market list and populate sidebar."""
        try:
            # Wait briefly for API connection
            for _ in range(10):
                if self._client.connected:
                    break
                await asyncio.sleep(0.5)

            self._markets = await self._client.get_markets(limit=30)

            market_data = []
            for m in self._markets:
                price = m.outcome_prices[0] if m.outcome_prices else 0.0
                market_data.append({
                    "condition_id": m.condition_id,
                    "token_id": m.yes_token,
                    "question": m.question,
                    "price": price,
                    "volume": m.volume,
                })

            market_list = self.query_one(MarketList)
            market_list.set_markets(market_data)

            # Auto-select first market
            if self._markets:
                first = self._markets[0]
                await self._select_market(
                    first.condition_id,
                    first.yes_token,
                    first.question,
                )

            logger.info("Loaded %d markets", len(self._markets))
        except Exception as e:
            logger.error("Failed to load markets: %s", e)

    # ── Market Selection ────────────────────────────────────────

    @on(MarketSelected)
    async def _on_market_selected(self, event: MarketSelected) -> None:
        await self._select_market(event.condition_id, event.token_id, event.question)

    async def _select_market(
        self, condition_id: str, token_id: str, question: str
    ) -> None:
        """Switch active market — update subscriptions and UI."""
        old_token = self._active_token
        self._active_token = token_id
        self._active_market = next(
            (m for m in self._markets if m.condition_id == condition_id), None
        )

        # Update status bar
        status = self.query_one(StatusBarWidget)
        status.market_name = question[:50] if len(question) > 50 else question

        # Unsubscribe old, subscribe new
        if old_token and old_token != token_id:
            await self._ws_mgr.unsubscribe([old_token])

        # Fetch initial order book
        self._fetch_initial_data(token_id)

        # Subscribe to WebSocket updates
        await self._ws_mgr.subscribe([token_id])
        if not self._ws_mgr.is_connected:
            await self._ws_mgr.start([token_id])
            status.ws_connected = True

    @work(thread=False)
    async def _fetch_initial_data(self, token_id: str) -> None:
        """Load initial book + price history for a newly selected market."""
        try:
            book = await self._client.get_order_book(token_id)
            self._data_store.update_book(token_id, book)

            # Load price history
            history = await self._client.get_price_history(token_id)
            if history:
                self._data_store.set_price_history(
                    token_id, [p.price for p in history]
                )

            # Immediate UI refresh
            self._refresh_book_ui()
            self._refresh_chart_ui()
        except Exception as e:
            logger.debug("Initial data fetch failed: %s", e)

    # ── WebSocket Callbacks ─────────────────────────────────────

    async def _handle_book_update(self, data: dict) -> None:
        """Process order book update from WebSocket."""
        token_id = data.get("asset_id", data.get("market", ""))
        if not token_id:
            return
        self._data_store.update_book_from_raw(token_id, data)

    async def _handle_trade(self, data: dict) -> None:
        """Process trade event from WebSocket."""
        token_id = data.get("asset_id", data.get("market", ""))
        if not token_id:
            return
        self._data_store.add_trade_from_raw(token_id, data)

    async def _handle_user_event(self, data: dict) -> None:
        """Process user channel events (fills, cancels)."""
        event = self._order_mgr.handle_user_event(data)
        if event:
            entry = self.query_one(OrderEntryPanel)
            if event.action == "filled":
                entry.set_status(f"Fill: {event.detail}", "bold green")
                # Update position tracker on fills
                side = data.get("side", "")
                price = float(data.get("price", 0))
                size = float(data.get("size", 0))
                token_id = data.get("asset_id", "")
                if token_id:
                    self._pos_tracker.handle_fill(token_id, side, price, size)
            elif event.action == "cancelled":
                entry.set_status(f"Cancelled: {event.order_id[:8]}", "yellow")

    # ── Order Execution ─────────────────────────────────────────

    @on(OrderSubmitted)
    async def _on_order_submitted(self, event: OrderSubmitted) -> None:
        """Handle order submission from the entry form."""
        if not self._active_token:
            entry = self.query_one(OrderEntryPanel)
            entry.set_status("No market selected", "bold red")
            return
        self._execute_order(event)

    @work(thread=False)
    async def _execute_order(self, event: OrderSubmitted) -> None:
        entry = self.query_one(OrderEntryPanel)
        try:
            success, result = await self._order_mgr.submit_order(
                token_id=self._active_token,
                side=event.side,
                price=event.price,
                size=event.size,
                order_type=event.order_type,
            )
            if success:
                entry.set_status(f"Order placed: {result[:12]}", "bold green")
            else:
                entry.set_status(f"Failed: {result}", "bold red")
        except Exception as e:
            entry.set_status(f"Error: {e}", "bold red")

    @on(CancelAllRequested)
    async def _on_cancel_all(self, event: CancelAllRequested) -> None:
        self._do_cancel_all()

    @work(thread=False)
    async def _do_cancel_all(self) -> None:
        entry = self.query_one(OrderEntryPanel)
        count = await self._order_mgr.cancel_all()
        entry.set_status(f"Cancelled {count} orders", "yellow")

    # ── Periodic UI Refresh ─────────────────────────────────────

    def _refresh_book_ui(self) -> None:
        """Update order book widget from data store."""
        if not self._active_token:
            return
        book = self._data_store.get_book(self._active_token)
        widget = self.query_one(OrderBookWidget)
        widget.set_book(book)

    def _refresh_chart_ui(self) -> None:
        """Update price chart widget from data store."""
        if not self._active_token:
            return
        prices = self._data_store.get_sparkline_data(
            self._active_token, self._config.chart_width
        )
        name = self._active_market.question if self._active_market else ""
        widget = self.query_one(PriceChartWidget)
        widget.set_data(prices, name)

    def _refresh_trades_ui(self) -> None:
        """Update trade tape from data store."""
        if not self._active_token:
            return
        trades = self._data_store.get_trades(self._active_token)
        widget = self.query_one(TradeTape)
        widget.set_trades(trades)

    def _refresh_positions_ui(self) -> None:
        """Update positions table from position tracker."""
        self._do_refresh_positions()

    @work(thread=False)
    async def _do_refresh_positions(self) -> None:
        await self._pos_tracker.refresh()
        positions = self._pos_tracker.positions
        summary = self._pos_tracker.get_pnl_summary()
        widget = self.query_one(PositionsTable)
        widget.set_positions(positions, summary)

    def _refresh_status_bar(self) -> None:
        """Update status bar with current state."""
        status = self.query_one(StatusBarWidget)
        status.ws_connected = self._ws_mgr.is_connected

    # ── Keybinding Actions ──────────────────────────────────────

    def action_focus_buy(self) -> None:
        entry = self.query_one(OrderEntryPanel)
        entry._select_buy()
        self.query_one("#price-input").focus()

    def action_focus_sell(self) -> None:
        entry = self.query_one(OrderEntryPanel)
        entry._select_sell()
        self.query_one("#price-input").focus()

    def action_refresh_positions(self) -> None:
        self._do_refresh_positions()

    def action_clear_form(self) -> None:
        try:
            self.query_one("#price-input").value = ""
            self.query_one("#size-input").value = ""
            entry = self.query_one(OrderEntryPanel)
            entry.set_status("", "dim")
        except Exception:
            pass


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        filename="polyterm.log",
    )
    app = PolyTermApp()
    app.run()
