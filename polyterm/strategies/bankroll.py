"""Bankroll manager — Kelly criterion position sizing for small accounts.

With $100, every trade matters. This module:
1. Calculates optimal bet size using Kelly criterion (or fractional Kelly)
2. Enforces maximum position sizes to prevent ruin
3. Tracks total exposure and warns when over-leveraged
4. Recommends passing on low-edge opportunities

Kelly formula: f* = (bp - q) / b
where:
  f* = fraction of bankroll to bet
  b  = odds received (net payout per dollar risked)
  p  = probability of winning
  q  = 1 - p (probability of losing)

For Polymarket: if you buy YES at price P, and you estimate
true probability is E:
  b = (1/P) - 1  (you pay P, get $1 if right, net gain = 1-P)
  p = E
  q = 1 - E
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass()
class BetRecommendation:
    """What the bankroll manager recommends for a specific opportunity."""

    should_bet: bool
    bet_size: float          # dollars to risk
    shares: float            # number of shares to buy
    expected_value: float    # expected profit in dollars
    kelly_fraction: float    # raw Kelly fraction
    applied_fraction: float  # fraction actually used (after fractional Kelly)
    max_loss: float          # worst case loss
    max_gain: float          # best case gain
    risk_rating: str         # "LOW", "MEDIUM", "HIGH"
    reason: str              # human-readable explanation

    @property
    def description(self) -> str:
        if not self.should_bet:
            return f"PASS — {self.reason}"
        return (
            f"BET ${self.bet_size:.2f} ({self.shares:.1f} shares)\n"
            f"  EV: ${self.expected_value:+.2f} | Risk: {self.risk_rating}\n"
            f"  Kelly: {self.kelly_fraction:.1%} → Applied: {self.applied_fraction:.1%}\n"
            f"  Max loss: ${self.max_loss:.2f} | Max gain: ${self.max_gain:.2f}"
        )


class BankrollManager:
    """Manages position sizing and risk for a small bankroll.

    Conservative by default — uses quarter-Kelly to protect against
    estimation errors and account blow-up.
    """

    def __init__(
        self,
        bankroll: float = 100.0,
        kelly_fraction: float = 0.25,   # quarter-Kelly (conservative)
        max_single_bet_pct: float = 0.15, # never more than 15% on one trade
        max_total_exposure_pct: float = 0.60, # never more than 60% deployed
        min_edge_to_bet: float = 0.03,   # need at least 3% edge
        min_bet_size: float = 1.0,       # minimum $1 bet (Polymarket minimum)
    ) -> None:
        self.bankroll = bankroll
        self.kelly_fraction = kelly_fraction
        self.max_single_bet_pct = max_single_bet_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.min_edge = min_edge_to_bet
        self.min_bet_size = min_bet_size
        self._current_exposure: float = 0.0
        self._open_bets: list[dict] = []

    @property
    def available_bankroll(self) -> float:
        """Cash available for new bets."""
        return self.bankroll - self._current_exposure

    @property
    def exposure_pct(self) -> float:
        """Current exposure as percentage of bankroll."""
        return self._current_exposure / self.bankroll if self.bankroll > 0 else 0

    @property
    def can_trade(self) -> bool:
        """Whether we have room for new trades."""
        return self.exposure_pct < self.max_total_exposure_pct

    def recommend(
        self,
        price: float,
        estimated_prob: float,
        confidence: str = "MEDIUM",
    ) -> BetRecommendation:
        """Calculate optimal position size for an opportunity.

        Args:
            price: current market price (what you'd pay per share)
            estimated_prob: our estimate of true probability
            confidence: "HIGH", "MEDIUM", or "LOW"
        """
        edge = estimated_prob - price

        # ── Reject low-edge opportunities ───────────────────────
        if edge < self.min_edge:
            return BetRecommendation(
                should_bet=False, bet_size=0, shares=0, expected_value=0,
                kelly_fraction=0, applied_fraction=0, max_loss=0, max_gain=0,
                risk_rating="N/A",
                reason=f"Edge too small: {edge:.1%} < {self.min_edge:.1%} minimum",
            )

        # ── Check exposure limits ───────────────────────────────
        if not self.can_trade:
            return BetRecommendation(
                should_bet=False, bet_size=0, shares=0, expected_value=0,
                kelly_fraction=0, applied_fraction=0, max_loss=0, max_gain=0,
                risk_rating="N/A",
                reason=f"Max exposure reached: {self.exposure_pct:.0%} of bankroll deployed",
            )

        # ── Calculate Kelly criterion ───────────────────────────
        # b = net odds = (1 - price) / price
        if price <= 0 or price >= 1:
            return BetRecommendation(
                should_bet=False, bet_size=0, shares=0, expected_value=0,
                kelly_fraction=0, applied_fraction=0, max_loss=0, max_gain=0,
                risk_rating="N/A",
                reason=f"Invalid price: {price}",
            )

        b = (1 - price) / price  # net odds
        p = estimated_prob
        q = 1 - p

        kelly_raw = (b * p - q) / b
        if kelly_raw <= 0:
            return BetRecommendation(
                should_bet=False, bet_size=0, shares=0, expected_value=0,
                kelly_fraction=kelly_raw, applied_fraction=0, max_loss=0, max_gain=0,
                risk_rating="N/A",
                reason=f"Negative Kelly ({kelly_raw:.1%}) — not a real edge",
            )

        # ── Apply fractional Kelly + confidence adjustment ──────
        confidence_multiplier = {
            "HIGH": 1.0,
            "MEDIUM": 0.7,
            "LOW": 0.4,
        }.get(confidence, 0.5)

        applied = kelly_raw * self.kelly_fraction * confidence_multiplier

        # ── Cap at maximum bet size ─────────────────────────────
        max_bet_frac = self.max_single_bet_pct
        max_available_frac = (self.available_bankroll / self.bankroll) if self.bankroll > 0 else 0
        applied = min(applied, max_bet_frac, max_available_frac)

        bet_dollars = self.bankroll * applied
        bet_dollars = max(bet_dollars, 0)

        # Round down to avoid over-spending
        shares = bet_dollars / price if price > 0 else 0

        # ── Enforce minimums ────────────────────────────────────
        if bet_dollars < self.min_bet_size:
            return BetRecommendation(
                should_bet=False, bet_size=0, shares=0, expected_value=0,
                kelly_fraction=kelly_raw, applied_fraction=applied,
                max_loss=0, max_gain=0,
                risk_rating="N/A",
                reason=f"Bet too small: ${bet_dollars:.2f} < ${self.min_bet_size:.2f} minimum",
            )

        # ── Calculate expected value ────────────────────────────
        ev = shares * (estimated_prob * (1 - price) - (1 - estimated_prob) * price)
        max_loss = bet_dollars
        max_gain = shares * (1 - price)

        # Risk rating
        if applied > 0.10:
            risk_rating = "HIGH"
        elif applied > 0.05:
            risk_rating = "MEDIUM"
        else:
            risk_rating = "LOW"

        return BetRecommendation(
            should_bet=True,
            bet_size=round(bet_dollars, 2),
            shares=round(shares, 1),
            expected_value=round(ev, 2),
            kelly_fraction=kelly_raw,
            applied_fraction=applied,
            max_loss=round(max_loss, 2),
            max_gain=round(max_gain, 2),
            risk_rating=risk_rating,
            reason="Kelly criterion with confidence adjustment",
        )

    # ── Exposure Tracking ───────────────────────────────────────

    def record_bet(self, token_id: str, cost: float) -> None:
        """Record a new bet for exposure tracking."""
        self._current_exposure += cost
        self._open_bets.append({"token_id": token_id, "cost": cost})

    def record_close(self, token_id: str, pnl: float) -> None:
        """Record a closed position."""
        for bet in self._open_bets:
            if bet["token_id"] == token_id:
                self._current_exposure -= bet["cost"]
                self.bankroll += pnl
                self._open_bets.remove(bet)
                break

    def update_bankroll(self, new_balance: float) -> None:
        """Sync bankroll with actual balance."""
        self.bankroll = new_balance

    def summary(self) -> str:
        """Human-readable bankroll summary."""
        return (
            f"Bankroll: ${self.bankroll:.2f}\n"
            f"Deployed: ${self._current_exposure:.2f} ({self.exposure_pct:.0%})\n"
            f"Available: ${self.available_bankroll:.2f}\n"
            f"Open bets: {len(self._open_bets)}"
        )
