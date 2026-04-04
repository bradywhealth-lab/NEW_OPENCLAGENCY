"""Price chart widget — ASCII sparkline with price annotations."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

# Block characters for 8-level vertical resolution
_BLOCKS = " \u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"
_LEVELS = len(_BLOCKS) - 1


class PriceChartWidget(Static):
    """ASCII sparkline price chart with annotations."""

    DEFAULT_CSS = """
    PriceChartWidget {
        height: 100%;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._prices: list[float] = []
        self._market_name: str = ""

    def set_data(self, prices: list[float], market_name: str = "") -> None:
        self._prices = prices
        self._market_name = market_name
        self.refresh()

    def render(self) -> Text:
        t = Text()
        t.append(" PRICE CHART\n", style="bold dim")

        prices = self._prices
        if len(prices) < 2:
            t.append("  Waiting for price data...", style="dim italic")
            return t

        # Compute chart dimensions
        width = min(len(prices), self.size.width - 4) if self.size.width > 4 else len(prices)
        data = prices[-width:] if len(prices) > width else prices

        p_min = min(data)
        p_max = max(data)
        p_range = p_max - p_min
        if p_range == 0:
            p_range = 0.01  # avoid division by zero

        last = data[-1]
        first = data[0]
        trend_up = last >= first

        # Header with price info
        change = last - first
        change_pct = (change / first * 100) if first else 0
        style = "bold green" if trend_up else "bold red"
        arrow = "\u25b2" if trend_up else "\u25bc"

        t.append(f"  {last:.4f} ", style=style)
        t.append(f"{arrow} {change:+.4f} ({change_pct:+.1f}%)\n", style=style)
        t.append(f"  H {p_max:.4f}  L {p_min:.4f}\n", style="dim")
        t.append("\n")

        # Build multi-row chart (8 rows)
        chart_height = min(8, max(self.size.height - 6, 4)) if self.size.height > 6 else 4

        for row in range(chart_height, 0, -1):
            # Price label for this row
            row_price = p_min + (row / chart_height) * p_range
            t.append(f"  {row_price:.2f}\u2502", style="dim")

            for val in data:
                normalized = (val - p_min) / p_range  # 0.0 to 1.0
                level_in_chart = normalized * chart_height

                # How much of this cell is filled
                cell_bottom = row - 1
                fill = level_in_chart - cell_bottom

                if fill >= 1.0:
                    char = _BLOCKS[_LEVELS]
                elif fill <= 0.0:
                    char = " "
                else:
                    idx = int(fill * _LEVELS)
                    char = _BLOCKS[max(1, idx)]

                color = "green" if trend_up else "red"
                t.append(char, style=color)

            t.append("\n")

        # X-axis
        t.append(f"  {'':>5}\u2514{'─' * len(data)}", style="dim")

        return t
