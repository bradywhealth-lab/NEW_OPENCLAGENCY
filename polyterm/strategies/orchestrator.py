"""Strategy orchestrator — runs all scanners and recommends/executes trades.

This is the brain of the bot. It:
1. Periodically runs all edge scanners
2. Filters and ranks opportunities by edge * confidence
3. Uses the bankroll manager to size positions
4. Either auto-executes or presents opportunities to the user
5. Logs every decision for review
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

from polyterm.strategies.arbitrage import ArbitrageOpportunity, ArbitrageScanner
from polyterm.strategies.bankroll import BankrollManager, BetRecommendation
from polyterm.strategies.crypto_edge import CryptoEdge, CryptoEdgeScanner
from polyterm.strategies.event_scanner import EventResolutionScanner, ResolutionEdge
from polyterm.strategies.weather_edge import WeatherEdge, WeatherEdgeScanner

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class TradeSignal:
    """A recommended trade from the orchestrator."""

    strategy: str        # "arbitrage", "crypto", "weather", "resolution"
    token_id: str
    side: str            # "BUY_YES", "BUY_NO", "BUY_BOTH"
    price: float
    edge: float
    edge_pct: float
    confidence: str      # "HIGH", "MEDIUM", "LOW"
    bet: BetRecommendation
    market_question: str
    detail: str          # strategy-specific detail
    timestamp: float = 0.0

    # For arbitrage (buy-both) trades
    second_token_id: str = ""
    second_price: float = 0.0

    @property
    def is_actionable(self) -> bool:
        return self.bet.should_bet

    @property
    def display(self) -> str:
        lines = [
            f"[{self.strategy.upper()}] {self.side} — {self.market_question[:45]}",
            f"  Edge: {self.edge_pct:+.1f}% | Confidence: {self.confidence}",
        ]
        if self.bet.should_bet:
            lines.append(
                f"  Recommend: ${self.bet.bet_size:.2f} ({self.bet.shares:.1f} shares) "
                f"→ EV ${self.bet.expected_value:+.2f}"
            )
        else:
            lines.append(f"  PASS: {self.bet.reason}")
        return "\n".join(lines)


OnSignal = Callable[[TradeSignal], Coroutine[Any, Any, None]]


class StrategyOrchestrator:
    """Runs all edge scanners and manages the trading pipeline."""

    def __init__(
        self,
        bankroll_manager: BankrollManager,
        on_signal: OnSignal | None = None,
        scan_interval_s: float = 60.0,     # scan every 60 seconds
        auto_execute: bool = False,
    ) -> None:
        self._bankroll = bankroll_manager
        self._on_signal = on_signal
        self._scan_interval = scan_interval_s
        self._auto_execute = auto_execute

        # Initialize scanners
        self._arb_scanner = ArbitrageScanner(min_edge=0.005, min_volume=500)
        self._crypto_scanner = CryptoEdgeScanner(min_edge=0.08, min_volume=2000)
        self._weather_scanner = WeatherEdgeScanner(min_edge=0.10, min_volume=500)
        self._event_scanner = EventResolutionScanner(min_edge=0.03, min_volume=500)

        self._signals: list[TradeSignal] = []
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_scan: float = 0.0
        self._scan_count: int = 0

    @property
    def signals(self) -> list[TradeSignal]:
        return list(self._signals)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_scan_time(self) -> float:
        return self._last_scan

    @property
    def scan_count(self) -> int:
        return self._scan_count

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Start the scanning loop."""
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())
        logger.info(
            "Strategy orchestrator started (interval: %.0fs, auto: %s)",
            self._scan_interval, self._auto_execute,
        )

    async def stop(self) -> None:
        """Stop the scanning loop and clean up."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._arb_scanner.close()
        await self._crypto_scanner.close()
        await self._weather_scanner.close()
        await self._event_scanner.close()
        logger.info("Strategy orchestrator stopped")

    async def scan_once(self) -> list[TradeSignal]:
        """Run all scanners once and return signals."""
        return await self._run_scan()

    # ── Scan Loop ───────────────────────────────────────────────

    async def _scan_loop(self) -> None:
        """Main scanning loop."""
        while self._running:
            try:
                signals = await self._run_scan()

                # Notify caller of actionable signals
                for signal in signals:
                    if signal.is_actionable and self._on_signal:
                        await self._on_signal(signal)

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Scan error: %s", e, exc_info=True)

            await asyncio.sleep(self._scan_interval)

    async def _run_scan(self) -> list[TradeSignal]:
        """Execute all scanners and rank results."""
        self._scan_count += 1
        start = time.time()
        signals: list[TradeSignal] = []

        # Run all scanners concurrently
        arb_task = asyncio.create_task(self._scan_arbitrage())
        crypto_task = asyncio.create_task(self._scan_crypto())
        weather_task = asyncio.create_task(self._scan_weather())
        event_task = asyncio.create_task(self._scan_events())

        arb_signals = await arb_task
        crypto_signals = await crypto_task
        weather_signals = await weather_task
        event_signals = await event_task

        signals = arb_signals + crypto_signals + weather_signals + event_signals

        # Sort by: confidence-adjusted edge (highest first)
        confidence_weights = {"HIGH": 3.0, "MEDIUM": 2.0, "LOW": 1.0}
        signals.sort(
            key=lambda s: abs(s.edge) * confidence_weights.get(s.confidence, 1.0),
            reverse=True,
        )

        self._signals = signals
        self._last_scan = time.time()
        elapsed = time.time() - start

        actionable = sum(1 for s in signals if s.is_actionable)
        logger.info(
            "Scan #%d complete in %.1fs: %d signals (%d actionable)",
            self._scan_count, elapsed, len(signals), actionable,
        )

        return signals

    # ── Individual Scanner Runners ──────────────────────────────

    async def _scan_arbitrage(self) -> list[TradeSignal]:
        """Run arbitrage scanner and convert to trade signals."""
        signals: list[TradeSignal] = []
        try:
            opps = await self._arb_scanner.scan()
            for opp in opps:
                if opp.is_buy_both:
                    # For arb, we "buy" at the combined price
                    bet = self._bankroll.recommend(
                        price=opp.combined_price,
                        estimated_prob=1.0,  # guaranteed $1 payout
                        confidence="HIGH",
                    )
                    signals.append(TradeSignal(
                        strategy="arbitrage",
                        token_id=opp.yes_token,
                        second_token_id=opp.no_token,
                        side="BUY_BOTH",
                        price=opp.combined_price,
                        second_price=opp.no_price,
                        edge=opp.edge,
                        edge_pct=opp.edge_pct,
                        confidence="HIGH",
                        bet=bet,
                        market_question=opp.market_question,
                        detail=opp.description,
                        timestamp=time.time(),
                    ))
        except Exception as e:
            logger.warning("Arbitrage scan failed: %s", e)
        return signals

    async def _scan_crypto(self) -> list[TradeSignal]:
        """Run crypto edge scanner."""
        signals: list[TradeSignal] = []
        try:
            edges = await self._crypto_scanner.scan()
            for edge in edges:
                price = edge.market_yes_price if edge.side == "BUY_YES" else (1 - edge.market_yes_price)
                bet = self._bankroll.recommend(
                    price=price,
                    estimated_prob=edge.estimated_prob if edge.side == "BUY_YES" else (1 - edge.estimated_prob),
                    confidence=edge.confidence,
                )
                signals.append(TradeSignal(
                    strategy="crypto",
                    token_id=edge.token_id,
                    side=edge.side,
                    price=price,
                    edge=abs(edge.edge),
                    edge_pct=abs(edge.edge_pct),
                    confidence=edge.confidence,
                    bet=bet,
                    market_question=edge.market_question,
                    detail=edge.description,
                    timestamp=time.time(),
                ))
        except Exception as e:
            logger.warning("Crypto scan failed: %s", e)
        return signals

    async def _scan_weather(self) -> list[TradeSignal]:
        """Run weather edge scanner."""
        signals: list[TradeSignal] = []
        try:
            edges = await self._weather_scanner.scan()
            for edge in edges:
                price = edge.market_yes_price if edge.side == "BUY_YES" else (1 - edge.market_yes_price)
                bet = self._bankroll.recommend(
                    price=price,
                    estimated_prob=edge.estimated_prob if edge.side == "BUY_YES" else (1 - edge.estimated_prob),
                    confidence=edge.confidence,
                )
                signals.append(TradeSignal(
                    strategy="weather",
                    token_id=edge.token_id,
                    side=edge.side,
                    price=price,
                    edge=abs(edge.edge),
                    edge_pct=abs(edge.edge_pct),
                    confidence=edge.confidence,
                    bet=bet,
                    market_question=edge.market_question,
                    detail=edge.description,
                    timestamp=time.time(),
                ))
        except Exception as e:
            logger.warning("Weather scan failed: %s", e)
        return signals

    async def _scan_events(self) -> list[TradeSignal]:
        """Run event resolution scanner."""
        signals: list[TradeSignal] = []
        try:
            edges = await self._event_scanner.scan()
            for edge in edges:
                bet = self._bankroll.recommend(
                    price=edge.current_price,
                    estimated_prob=0.98 if edge.confidence == "HIGH" else 0.90,
                    confidence=edge.confidence,
                )
                signals.append(TradeSignal(
                    strategy="resolution",
                    token_id=edge.token_id,
                    side=edge.side,
                    price=edge.current_price,
                    edge=edge.edge,
                    edge_pct=edge.edge_pct,
                    confidence=edge.confidence,
                    bet=bet,
                    market_question=edge.market_question,
                    detail=edge.description,
                    timestamp=time.time(),
                ))
        except Exception as e:
            logger.warning("Event scan failed: %s", e)
        return signals
