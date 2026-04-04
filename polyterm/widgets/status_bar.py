"""Status bar — connection state, selected market, balance, time."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


class StatusBarWidget(Static):
    """Top status bar showing connection, market, and account info."""

    market_name: reactive[str] = reactive("No market selected")
    connected: reactive[bool] = reactive(False)
    balance: reactive[float] = reactive(0.0)
    ws_connected: reactive[bool] = reactive(False)

    def render(self) -> Text:
        t = Text()

        # Connection indicator
        if self.connected:
            t.append(" \u25cf ", style="bold green")
            t.append("API ", style="green")
        else:
            t.append(" \u25cf ", style="bold red")
            t.append("API ", style="red")

        if self.ws_connected:
            t.append("\u25cf ", style="bold green")
            t.append("WS", style="green")
        else:
            t.append("\u25cf ", style="bold red")
            t.append("WS", style="red")

        t.append("  \u2502  ", style="dim")

        # Market name
        t.append(self.market_name, style="bold white")

        t.append("  \u2502  ", style="dim")

        # Balance
        t.append(f"${self.balance:,.2f}", style="bold cyan")

        t.append("  \u2502  ", style="dim")

        # Timestamp
        now = datetime.now().strftime("%H:%M:%S")
        t.append(now, style="dim")

        return t
