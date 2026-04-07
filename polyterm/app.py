"""PolyTerm — Lightweight Polymarket CLOB Trading Terminal.

Single-screen TUI with real-time price feeds, L2 order book,
ASCII charts, live P&L, and instant order execution.
Supports paper trading (dry run) and copy trading modes.
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
from polyterm.core.copy_trader import CopyTradeConfig, CopyTradeEvent, CopyTrader
from polyterm.core.market_data import MarketDataStore
from polyterm.core.order_manager import OrderManager
from polyterm.core.paper_engine import PaperTradingEngine
from polyterm.core.position_tracker import PnLSummary, PositionTracker
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

        # Paper trading engine
        self._paper: PaperTradingEngine | None = None
        if self._config.paper_trading:
            self._paper = PaperTradingEngine(
                self._data_store,
                starting_balance=self._config.paper_balance,
            )

        # Copy trader
        self._copy_trader: CopyTrader | None = None
        if self._config.copy_enabled and self._config.copy_target:
            copy_cfg = CopyTradeConfig(
                target_address=self._config.copy_target,
                size_multiplier=self._config.copy_multiplier,
                max_position_size=self._config.copy_max_size,
                auto_execute=self._config.copy_auto_execute,
                poll_interval_s=self._config.copy_poll_interval,
            )
            self._copy_trader = CopyTrader(
                config=copy_cfg,
                on_trade=self._handle_copy_trade,
            )

    # ── Layout ──────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield StatusBarWidget()
        yield MarketList()
        yield PriceChartWidget()
        yield OrderEntryPanel()
        yield OrderBookWidget(depth=self._config.book_depth)
        yield TradeTape()
        yield PositionsTable()

    # ── Lifecycle ───────────────────────────────────────────────

    async def on_mount(self) -> None:
        """Initialize connections and start data feeds."""
        self._connect_api()
        self._load_markets()

        # Configure status bar for trading mode
        status = self.query_one(StatusBarWidget)
        status.trading_mode = self._config.trading_mode

        if self._paper:
            status.balance = self._paper.balance
            entry = self.query_one(OrderEntryPanel)
            entry.set_status("PAPER MODE — simulated execution", "bold yellow")

        if self._copy_trader:
            status.copy_active = True
            status.copy_target = self._config.copy_target

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

        # Paper trading: periodic check for limit order fills
        if self._paper:
            self.set_interval(0.25, self._check_paper_fills)

        # Start copy trader if configured
        if self._copy_trader:
            self._start_copy_trader()

    async def on_unmount(self) -> None:
        """Clean shutdown."""
        if self._copy_trader:
            await self._copy_trader.stop()
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

        status = self.query_one(StatusBarWidget)
        status.market_name = question[:50] if len(question) > 50 else question

        if old_token and old_token != token_id:
            await self._ws_mgr.unsubscribe([old_token])

        self._fetch_initial_data(token_id)

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

            history = await self._client.get_price_history(token_id)
            if history:
                self._data_store.set_price_history(
                    token_id, [p.price for p in history]
                )

            self._refresh_book_ui()
            self._refresh_chart_ui()
        except Exception as e:
            logger.debug("Initial data fetch failed: %s", e)

    # ── WebSocket Callbacks ─────────────────────────────────────

    async def _handle_book_update(self, data: dict) -> None:
        token_id = data.get("asset_id", data.get("market", ""))
        if not token_id:
            return
        self._data_store.update_book_from_raw(token_id, data)

    async def _handle_trade(self, data: dict) -> None:
        token_id = data.get("asset_id", data.get("market", ""))
        if not token_id:
            return
        self._data_store.add_trade_from_raw(token_id, data)

    async def _handle_user_event(self, data: dict) -> None:
        event = self._order_mgr.handle_user_event(data)
        if event:
            entry = self.query_one(OrderEntryPanel)
            if event.action == "filled":
                entry.set_status(f"Fill: {event.detail}", "bold green")
                side = data.get("side", "")
                price = float(data.get("price", 0))
                size = float(data.get("size", 0))
                token_id = data.get("asset_id", "")
                if token_id:
                    self._pos_tracker.handle_fill(token_id, side, price, size)
            elif event.action == "cancelled":
                entry.set_status(f"Cancelled: {event.order_id[:8]}", "yellow")

    # ── Order Execution (Paper + Live) ──────────────────────────

    @on(OrderSubmitted)
    async def _on_order_submitted(self, event: OrderSubmitted) -> None:
        if not self._active_token:
            entry = self.query_one(OrderEntryPanel)
            entry.set_status("No market selected", "bold red")
            return

        if self._paper:
            self._execute_paper_order(event)
        else:
            self._execute_order(event)

    def _execute_paper_order(self, event: OrderSubmitted) -> None:
        """Execute order through paper trading engine."""
        entry = self.query_one(OrderEntryPanel)
        success, result = self._paper.place_order(
            token_id=self._active_token,
            side=event.side,
            price=event.price,
            size=event.size,
            order_type=event.order_type,
        )
        if success:
            entry.set_status(
                f"[PAPER] Order placed: {result[:16]} | Bal: ${self._paper.balance:,.2f}",
                "bold green",
            )
        else:
            entry.set_status(f"[PAPER] Failed: {result}", "bold red")

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
        if self._paper:
            count = self._paper.cancel_all()
            entry = self.query_one(OrderEntryPanel)
            entry.set_status(f"[PAPER] Cancelled {count} orders", "yellow")
        else:
            self._do_cancel_all()

    @work(thread=False)
    async def _do_cancel_all(self) -> None:
        entry = self.query_one(OrderEntryPanel)
        count = await self._order_mgr.cancel_all()
        entry.set_status(f"Cancelled {count} orders", "yellow")

    # ── Paper Trading Periodic Check ────────────────────────────

    def _check_paper_fills(self) -> None:
        """Check if any pending paper limit orders should fill."""
        if not self._paper:
            return
        new_fills = self._paper.check_pending_orders()
        if new_fills:
            entry = self.query_one(OrderEntryPanel)
            for fill in new_fills:
                entry.set_status(
                    f"[PAPER] FILL: {fill.side.value} {fill.size:.1f} @ {fill.price:.4f}",
                    "bold green",
                )

    # ── Copy Trading ────────────────────────────────────────────

    @work(thread=False)
    async def _start_copy_trader(self) -> None:
        """Start the copy trader background task."""
        if self._copy_trader:
            await self._copy_trader.start()

    async def _handle_copy_trade(self, event: CopyTradeEvent) -> None:
        """Handle a copy trade signal from the copy trader."""
        entry = self.query_one(OrderEntryPanel)

        if self._config.copy_auto_execute:
            # Auto-execute the trade
            if self._paper:
                success, result = self._paper.place_order(
                    token_id=event.token_id,
                    side=event.side,
                    price=event.price,
                    size=event.size,
                )
                status = "OK" if success else result
                entry.set_status(
                    f"[COPY/PAPER] {event.side.value} {event.size:.1f} @ {event.price:.4f} — {status}",
                    "bold magenta",
                )
            elif self._config.has_credentials:
                try:
                    success, result = await self._order_mgr.submit_order(
                        token_id=event.token_id,
                        side=event.side,
                        price=event.price,
                        size=event.size,
                    )
                    status = result[:12] if success else f"FAIL: {result}"
                    entry.set_status(
                        f"[COPY] {event.side.value} {event.size:.1f} @ {event.price:.4f} — {status}",
                        "bold magenta",
                    )
                except Exception as e:
                    entry.set_status(f"[COPY] Error: {e}", "bold red")
        else:
            # Notify user and pre-fill the order form
            entry.set_price(event.price)
            try:
                size_input = self.query_one("#size-input")
                size_input.value = f"{event.size:.1f}"
            except Exception:
                pass

            if event.side == Side.BUY:
                entry._select_buy()
            else:
                entry._select_sell()

            short_q = event.market_question[:30]
            entry.set_status(
                f"[COPY SIGNAL] {event.side.value} {event.size:.1f} @ {event.price:.4f} — {short_q}",
                "bold magenta",
            )
            self.notify(
                f"Copy signal: {event.side.value} {event.size:.1f} @ {event.price:.4f}\n{short_q}",
                title="Copy Trade",
                severity="warning",
                timeout=10,
            )

    # ── Periodic UI Refresh ─────────────────────────────────────

    def _refresh_book_ui(self) -> None:
        if not self._active_token:
            return
        book = self._data_store.get_book(self._active_token)
        widget = self.query_one(OrderBookWidget)
        widget.set_book(book)

    def _refresh_chart_ui(self) -> None:
        if not self._active_token:
            return
        prices = self._data_store.get_sparkline_data(
            self._active_token, self._config.chart_width
        )
        name = self._active_market.question if self._active_market else ""
        widget = self.query_one(PriceChartWidget)
        widget.set_data(prices, name)

    def _refresh_trades_ui(self) -> None:
        if not self._active_token:
            return
        trades = self._data_store.get_trades(self._active_token)
        widget = self.query_one(TradeTape)
        widget.set_trades(trades)

    def _refresh_positions_ui(self) -> None:
        if self._paper:
            self._refresh_paper_positions()
        else:
            self._do_refresh_positions()

    def _refresh_paper_positions(self) -> None:
        """Refresh positions from paper trading engine."""
        if not self._paper:
            return
        self._paper.update_mark_prices()
        positions = self._paper.positions
        unrealized = sum(p.unrealized_pnl for p in positions)
        summary = PnLSummary(
            unrealized=round(unrealized, 4),
            realized=round(self._paper.realized_pnl, 4),
            total=round(unrealized + self._paper.realized_pnl, 4),
            position_count=len(positions),
        )
        widget = self.query_one(PositionsTable)
        widget.set_positions(positions, summary)

    @work(thread=False)
    async def _do_refresh_positions(self) -> None:
        await self._pos_tracker.refresh()
        positions = self._pos_tracker.positions
        summary = self._pos_tracker.get_pnl_summary()
        widget = self.query_one(PositionsTable)
        widget.set_positions(positions, summary)

    def _refresh_status_bar(self) -> None:
        status = self.query_one(StatusBarWidget)
        status.ws_connected = self._ws_mgr.is_connected
        if self._paper:
            status.balance = self._paper.balance
        if self._copy_trader:
            status.copy_active = self._copy_trader.is_running

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
        if self._paper:
            self._refresh_paper_positions()
        else:
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
