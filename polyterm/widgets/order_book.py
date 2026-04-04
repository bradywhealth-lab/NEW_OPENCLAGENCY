"""Order book widget — L2 depth visualization with bar charts."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from polyterm.api.models import OrderBook

_BAR_WIDTH = 10
_FULL = "\u2593"
_EMPTY = "\u2591"


class OrderBookWidget(Static):
    """Displays L2 order book with visual depth bars."""

    DEFAULT_CSS = """
    OrderBookWidget {
        height: 100%;
        padding: 0;
    }
    """

    def __init__(self, depth: int = 10, **kwargs) -> None:
        super().__init__(**kwargs)
        self._depth = depth
        self._book = OrderBook()

    def set_book(self, book: OrderBook) -> None:
        self._book = book
        self.refresh()

    def render(self) -> Text:
        t = Text()
        t.append(" ORDER BOOK\n", style="bold dim")

        book = self._book
        asks = book.asks[: self._depth]
        bids = book.bids[: self._depth]

        if not asks and not bids:
            t.append("  Waiting for data...", style="dim italic")
            return t

        # Find max size for bar scaling
        all_sizes = [l.size for l in asks + bids]
        max_size = max(all_sizes) if all_sizes else 1.0

        # Asks (reversed so lowest ask is near spread)
        for level in reversed(asks):
            bar_len = int((level.size / max_size) * _BAR_WIDTH)
            bar = _FULL * bar_len + _EMPTY * (_BAR_WIDTH - bar_len)
            size_str = f"{level.size:>8.1f}"
            t.append(f"  {level.price:.4f} ", style="bold red")
            t.append(f"{bar} ", style="red")
            t.append(f"{size_str}\n", style="dim")

        # Spread line
        spread = book.spread
        mid = book.midpoint
        if mid > 0:
            t.append(f"  {'─' * 4} {mid:.4f} ", style="dim cyan")
            t.append(f"spread {spread:.4f}\n", style="dim")
        else:
            t.append(f"  {'─' * 20}\n", style="dim")

        # Bids
        for level in bids:
            bar_len = int((level.size / max_size) * _BAR_WIDTH)
            bar = _FULL * bar_len + _EMPTY * (_BAR_WIDTH - bar_len)
            size_str = f"{level.size:>8.1f}"
            t.append(f"  {level.price:.4f} ", style="bold green")
            t.append(f"{bar} ", style="green")
            t.append(f"{size_str}\n", style="dim")

        return t
