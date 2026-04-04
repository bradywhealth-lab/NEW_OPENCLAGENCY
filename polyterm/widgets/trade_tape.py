"""Trade tape — recent trades feed as a scrolling table."""

from __future__ import annotations

from datetime import datetime

from textual.widgets import DataTable, Static

from polyterm.api.models import Side, Trade


class TradeTape(Static):
    """Recent trades display with color-coded buy/sell."""

    DEFAULT_CSS = """
    TradeTape {
        height: 100%;
    }
    TradeTape DataTable {
        height: 1fr;
    }
    TradeTape .panel-title {
        height: 1;
    }
    """

    def compose(self):
        yield Static(" TRADES", classes="panel-title")
        table = DataTable(id="trade-table", zebra_stripes=True)
        table.cursor_type = "none"
        yield table

    def on_mount(self) -> None:
        table = self.query_one("#trade-table", DataTable)
        table.add_columns("Time", "Price", "Size", "Side")

    def set_trades(self, trades: list[Trade]) -> None:
        """Replace all trades in the table."""
        table = self.query_one("#trade-table", DataTable)
        table.clear()

        for trade in trades[:30]:  # Show last 30
            time_str = datetime.fromtimestamp(trade.timestamp).strftime("%H:%M:%S")
            price_str = f"{trade.price:.4f}"
            size_str = f"{trade.size:.1f}"
            side_str = trade.side.value

            style = "green" if trade.side == Side.BUY else "red"

            table.add_row(
                time_str,
                f"[{style}]{price_str}[/]",
                size_str,
                f"[{style} bold]{side_str}[/]",
            )

    def add_trade(self, trade: Trade) -> None:
        """Prepend a single new trade to the tape."""
        table = self.query_one("#trade-table", DataTable)
        time_str = datetime.fromtimestamp(trade.timestamp).strftime("%H:%M:%S")
        price_str = f"{trade.price:.4f}"
        size_str = f"{trade.size:.1f}"
        side_str = trade.side.value
        style = "green" if trade.side == Side.BUY else "red"

        # Insert at top
        table.add_row(
            time_str,
            f"[{style}]{price_str}[/]",
            size_str,
            f"[{style} bold]{side_str}[/]",
            key=f"trade-{trade.trade_id or trade.timestamp}",
        )

        # Trim old trades
        while table.row_count > 50:
            table.remove_row(table.rows[-1].key)
