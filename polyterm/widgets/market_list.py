"""Market list — watchlist sidebar for selecting active markets."""

from __future__ import annotations

from textual import on
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Label, ListItem, ListView, Static


class MarketSelected(Message):
    """Posted when user selects a market from the list."""

    def __init__(self, condition_id: str, token_id: str, question: str) -> None:
        super().__init__()
        self.condition_id = condition_id
        self.token_id = token_id
        self.question = question


class MarketListItem(ListItem):
    """Single market entry in the watchlist."""

    def __init__(
        self,
        condition_id: str,
        token_id: str,
        question: str,
        price: float,
        volume: float,
    ) -> None:
        super().__init__()
        self.condition_id = condition_id
        self.token_id = token_id
        self.question = question
        self.price = price
        self.volume = volume

    def compose(self):
        slug = self.question[:18] + ".." if len(self.question) > 18 else self.question
        price_str = f"{self.price:.0%}" if self.price else "---"
        vol_str = f"${self.volume / 1000:.0f}k" if self.volume >= 1000 else f"${self.volume:.0f}"
        yield Label(f"{slug}\n[dim]{price_str}  {vol_str}[/]", markup=True)


class MarketList(Static):
    """Watchlist panel showing available markets."""

    DEFAULT_CSS = """
    MarketList {
        height: 100%;
    }
    MarketList ListView {
        height: 1fr;
    }
    MarketList .panel-title {
        height: 1;
    }
    MarketList ListItem {
        height: 3;
        padding: 0 1;
    }
    MarketList ListItem:hover {
        background: $surface-lighten-1;
    }
    """

    def compose(self):
        yield Static(" MARKETS", classes="panel-title")
        yield ListView(id="market-listview")

    def set_markets(self, markets: list[dict]) -> None:
        """Populate the market list from market data."""
        lv = self.query_one("#market-listview", ListView)
        lv.clear()
        for m in markets:
            item = MarketListItem(
                condition_id=m["condition_id"],
                token_id=m["token_id"],
                question=m["question"],
                price=m.get("price", 0.0),
                volume=m.get("volume", 0.0),
            )
            lv.append(item)

    @on(ListView.Selected, "#market-listview")
    def _on_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, MarketListItem):
            self.post_message(
                MarketSelected(
                    condition_id=item.condition_id,
                    token_id=item.token_id,
                    question=item.question,
                )
            )
