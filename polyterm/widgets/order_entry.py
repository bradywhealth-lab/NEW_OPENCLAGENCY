"""Order entry panel — form for placing buy/sell orders."""

from __future__ import annotations

from textual import on
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Button, Input, Label, Select, Static

from polyterm.api.models import OrderType, Side


class OrderSubmitted(Message):
    """Posted when user submits an order."""

    def __init__(
        self, side: Side, price: float, size: float, order_type: OrderType
    ) -> None:
        super().__init__()
        self.side = side
        self.price = price
        self.size = size
        self.order_type = order_type


class CancelAllRequested(Message):
    """Posted when user requests cancelling all orders."""
    pass


class OrderEntryPanel(Static):
    """Order entry form with side toggle, price, size, and submit."""

    DEFAULT_CSS = """
    OrderEntryPanel {
        height: 100%;
        padding: 1;
    }
    OrderEntryPanel Horizontal {
        height: auto;
        margin: 0 0 1 0;
    }
    OrderEntryPanel .side-buttons {
        height: 3;
    }
    OrderEntryPanel Input {
        margin: 0 0 1 0;
    }
    OrderEntryPanel Label {
        height: 1;
        padding: 0;
        color: $text-muted;
    }
    OrderEntryPanel .active-buy {
        background: $success;
        color: white;
        text-style: bold;
    }
    OrderEntryPanel .active-sell {
        background: $error;
        color: white;
        text-style: bold;
    }
    OrderEntryPanel .inactive-side {
        background: $surface-darken-1;
        color: $text-muted;
    }
    OrderEntryPanel #submit-btn {
        width: 100%;
        margin: 1 0 0 0;
        text-style: bold;
    }
    OrderEntryPanel #cancel-all-btn {
        width: 100%;
        margin: 1 0 0 0;
        background: $surface-darken-1;
    }
    OrderEntryPanel #order-status {
        height: auto;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._side = Side.BUY

    def compose(self):
        yield Static(" ORDER ENTRY", classes="panel-title")
        with Horizontal(classes="side-buttons"):
            yield Button("BUY", id="buy-btn", classes="active-buy", variant="success")
            yield Button("SELL", id="sell-btn", classes="inactive-side")

        yield Label("Price")
        yield Input(placeholder="0.50", id="price-input", type="number")

        yield Label("Size")
        yield Input(placeholder="100", id="size-input", type="number")

        yield Label("Type")
        yield Select(
            [(t.value, t.value) for t in OrderType],
            value=OrderType.GTC.value,
            id="type-select",
        )

        yield Button(
            "SUBMIT ORDER",
            id="submit-btn",
            variant="success",
        )
        yield Button(
            "CANCEL ALL",
            id="cancel-all-btn",
            variant="error",
        )
        yield Static("", id="order-status")

    def set_price(self, price: float) -> None:
        """Pre-fill the price field."""
        inp = self.query_one("#price-input", Input)
        inp.value = f"{price:.4f}"

    def set_status(self, text: str, style: str = "dim") -> None:
        """Update the status message below the form."""
        status = self.query_one("#order-status", Static)
        status.update(f"[{style}]{text}[/]")

    @on(Button.Pressed, "#buy-btn")
    def _select_buy(self) -> None:
        self._side = Side.BUY
        self.query_one("#buy-btn", Button).set_classes("active-buy")
        self.query_one("#sell-btn", Button).set_classes("inactive-side")
        self.query_one("#submit-btn", Button).variant = "success"
        self.query_one("#submit-btn", Button).label = "SUBMIT BUY"

    @on(Button.Pressed, "#sell-btn")
    def _select_sell(self) -> None:
        self._side = Side.SELL
        self.query_one("#sell-btn", Button).set_classes("active-sell")
        self.query_one("#buy-btn", Button).set_classes("inactive-side")
        self.query_one("#submit-btn", Button).variant = "error"
        self.query_one("#submit-btn", Button).label = "SUBMIT SELL"

    @on(Button.Pressed, "#submit-btn")
    def _submit(self) -> None:
        price_input = self.query_one("#price-input", Input)
        size_input = self.query_one("#size-input", Input)
        type_select = self.query_one("#type-select", Select)

        try:
            price = float(price_input.value)
            size = float(size_input.value)
        except (ValueError, TypeError):
            self.set_status("Invalid price or size", "bold red")
            return

        if price <= 0 or price >= 1:
            self.set_status("Price must be 0 < p < 1", "bold red")
            return
        if size <= 0:
            self.set_status("Size must be > 0", "bold red")
            return

        order_type = OrderType(type_select.value) if type_select.value else OrderType.GTC

        self.set_status("Submitting...", "bold yellow")
        self.post_message(
            OrderSubmitted(
                side=self._side,
                price=price,
                size=size,
                order_type=order_type,
            )
        )

    @on(Button.Pressed, "#cancel-all-btn")
    def _cancel_all(self) -> None:
        self.post_message(CancelAllRequested())
