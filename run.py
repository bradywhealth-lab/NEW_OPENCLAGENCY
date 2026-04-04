#!/usr/bin/env python3
"""Entry point for Polymarket Trading Terminal."""

from polyterm.app import PolyTermApp


def main() -> None:
    app = PolyTermApp()
    app.run()


if __name__ == "__main__":
    main()
