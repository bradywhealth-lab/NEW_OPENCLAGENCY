"""Status bar — connection state, trading mode, copy trade, balance, time."""

from __future__ import annotations

from datetime import datetime

from rich.text import Text
from textual.reactive import reactive
from textual.widgets import Static


class StatusBarWidget(Static):
    """Top status bar showing connection, mode, market, and account info."""

    market_name: reactive[str] = reactive("No market selected")
    connected: reactive[bool] = reactive(False)
    balance: reactive[float] = reactive(0.0)
    ws_connected: reactive[bool] = reactive(False)
    trading_mode: reactive[str] = reactive("READ-ONLY")
    copy_active: reactive[bool] = reactive(False)
    copy_target: reactive[str] = reactive("")

    def render(self) -> Text:
        t = Text()

        # Trading mode badge
        mode = self.trading_mode
        if mode == "PAPER":
            t.append(" PAPER ", style="bold black on yellow")
        elif mode == "LIVE":
            t.append(" LIVE ", style="bold white on red")
        else:
            t.append(" VIEW ", style="bold white on blue")

        t.append(" ", style="")

        # Connection indicators
        if self.connected:
            t.append("\u25cf", style="bold green")
            t.append("API ", style="green")
        else:
            t.append("\u25cf", style="bold red")
            t.append("API ", style="red")

        if self.ws_connected:
            t.append("\u25cf", style="bold green")
            t.append("WS", style="green")
        else:
            t.append("\u25cf", style="bold red")
            t.append("WS", style="red")

        t.append("  \u2502  ", style="dim")

        # Market name
        t.append(self.market_name, style="bold white")

        t.append("  \u2502  ", style="dim")

        # Balance
        t.append(f"${self.balance:,.2f}", style="bold cyan")

        # Copy trade indicator
        if self.copy_active:
            t.append("  \u2502  ", style="dim")
            t.append("\u25cf ", style="bold magenta")
            short_addr = self.copy_target[:6] + ".." + self.copy_target[-4:] if len(self.copy_target) > 10 else self.copy_target
            t.append(f"COPY:{short_addr}", style="magenta")

        t.append("  \u2502  ", style="dim")

        # Timestamp
        now = datetime.now().strftime("%H:%M:%S")
        t.append(now, style="dim")

        return t
