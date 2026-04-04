"""Positions table — open positions with live P&L tracking."""

from __future__ import annotations

from textual.widgets import DataTable, Static

from polyterm.api.models import Position
from polyterm.core.position_tracker import PnLSummary


class PositionsTable(Static):
    """Displays open positions with unrealized P&L."""

    DEFAULT_CSS = """
    PositionsTable {
        height: 100%;
    }
    PositionsTable DataTable {
        height: 1fr;
    }
    PositionsTable .panel-title {
        height: 1;
    }
    PositionsTable #pnl-summary {
        height: 2;
        padding: 0 1;
        background: $surface-darken-1;
    }
    """

    def compose(self):
        yield Static(" POSITIONS & P&L", classes="panel-title")
        table = DataTable(id="positions-table", zebra_stripes=True)
        table.cursor_type = "none"
        yield table
        yield Static("", id="pnl-summary")

    def on_mount(self) -> None:
        table = self.query_one("#positions-table", DataTable)
        table.add_columns("Market", "Side", "Size", "Entry", "Current", "P&L", "P&L %")

    def set_positions(self, positions: list[Position], summary: PnLSummary) -> None:
        """Update positions table and P&L summary."""
        table = self.query_one("#positions-table", DataTable)
        table.clear()

        for pos in positions:
            pnl = pos.unrealized_pnl
            pnl_pct = pos.pnl_percent
            pnl_style = "green" if pnl >= 0 else "red"

            market_label = pos.market_question[:20] if pos.market_question else pos.token_id[:12]
            outcome = pos.outcome or "Yes"

            table.add_row(
                f"{market_label}",
                f"{outcome}",
                f"{pos.size:.1f}",
                f"{pos.avg_price:.4f}",
                f"{pos.current_price:.4f}",
                f"[{pnl_style}]{pnl:+.2f}[/]",
                f"[{pnl_style}]{pnl_pct:+.1f}%[/]",
            )

        # Update P&L summary
        summary_widget = self.query_one("#pnl-summary", Static)
        u_style = "bold green" if summary.unrealized >= 0 else "bold red"
        r_style = "bold green" if summary.realized >= 0 else "bold red"
        t_style = "bold green" if summary.total >= 0 else "bold red"

        summary_widget.update(
            f"[{u_style}]Unrealized: ${summary.unrealized:+.2f}[/]  "
            f"[{r_style}]Realized: ${summary.realized:+.2f}[/]  "
            f"[{t_style}]Total: ${summary.total:+.2f}[/]  "
            f"[dim]({summary.position_count} positions)[/]"
        )
