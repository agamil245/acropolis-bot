"""
Panic Reversal Strategy (Layer 4) — Asymmetric Lottery Bets on Extreme Prices.

Inspired by Atlas/@crptAtlas:
  When BTC moves fast, crowds panic-sell one side down to $0.03–$0.07.
  When BTC snaps back, those contracts reprice instantly → 14x–33x payoff.
  Only need to hit 1 out of 20 to break even. Historical rate is ~1 out of 10.

Strategy:
  - Scan active 5-min markets for extreme prices (any side < $0.10)
  - Buy the cheap side with small fixed bets ($2-5)
  - Hold to settlement OR take profit at 3x entry
  - The math: 20 attempts × $5 = $100 risk. If 2 hit at 20x = $200. Net +$100.

This is a pure asymmetric payoff strategy. Most bets lose.
The few that hit pay 10-33x. Net positive over time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from src.config import Config, MarketType

if TYPE_CHECKING:
    from src.core.polymarket import Market
    from src.strategies.bayesian_model import BayesianModel


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ReversalSignal:
    """Signal emitted when an extreme price opportunity is detected."""
    market: "Market"
    cheap_side: str                 # "up" or "down"
    price: float                    # current price of cheap side (e.g. 0.05)
    potential_multiplier: float     # 1/price (e.g. 20x at $0.05)
    time_left_seconds: int          # seconds until window closes
    volatility_regime: str          # current vol state
    mean_reversion_score: float     # 0-1, how likely is a reversal
    recommended_size: float         # USD to bet
    timestamp: float = 0.0

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()

    @property
    def token_id(self) -> Optional[str]:
        """Token ID for the cheap side."""
        if self.cheap_side == "up":
            return self.market.up_token_id
        return self.market.down_token_id


@dataclass
class PanicPosition:
    """Tracks an active panic reversal position."""
    market_slug: str
    market_timestamp: int
    market_type: MarketType
    cheap_side: str
    entry_price: float
    amount_usd: float
    shares: float                   # amount / entry_price
    entry_time: float
    token_id: Optional[str] = None
    current_price: float = 0.0
    settled: bool = False
    won: bool = False
    pnl: float = 0.0
    exit_reason: str = ""           # "settlement", "take_profit", "expired"

    @property
    def unrealized_pnl(self) -> float:
        if self.current_price > 0:
            return (self.current_price - self.entry_price) * self.shares
        return 0.0

    @property
    def current_multiplier(self) -> float:
        if self.entry_price > 0 and self.current_price > 0:
            return self.current_price / self.entry_price
        return 0.0


@dataclass
class PanicReversalStats:
    """Cumulative statistics for the panic reversal strategy."""
    attempts: int = 0
    hits: int = 0
    misses: int = 0
    total_spent: float = 0.0
    total_won: float = 0.0
    best_multiplier: float = 0.0
    best_single_pnl: float = 0.0
    multiplier_sum_on_wins: float = 0.0
    daily_spend: float = 0.0
    daily_reset_date: str = ""

    @property
    def hit_rate(self) -> float:
        settled = self.hits + self.misses
        return self.hits / settled if settled > 0 else 0.0

    @property
    def net_pnl(self) -> float:
        return self.total_won - self.total_spent

    @property
    def avg_multiplier_on_wins(self) -> float:
        return self.multiplier_sum_on_wins / self.hits if self.hits > 0 else 0.0

    def reset_daily_if_needed(self):
        """Reset daily spend counter at midnight."""
        today = time.strftime("%Y-%m-%d")
        if self.daily_reset_date != today:
            self.daily_spend = 0.0
            self.daily_reset_date = today

    def record_attempt(self, amount: float):
        self.reset_daily_if_needed()
        self.attempts += 1
        self.total_spent += amount
        self.daily_spend += amount

    def record_settlement(self, won: bool, pnl: float, multiplier: float = 0.0):
        if won:
            self.hits += 1
            self.total_won += pnl + self.total_spent / max(self.attempts, 1)  # approximate cost basis
            self.multiplier_sum_on_wins += multiplier
            if multiplier > self.best_multiplier:
                self.best_multiplier = multiplier
            if pnl > self.best_single_pnl:
                self.best_single_pnl = pnl
        else:
            self.misses += 1

    def to_dict(self) -> dict:
        return {
            "attempts": self.attempts,
            "hits": self.hits,
            "misses": self.misses,
            "hit_rate": f"{self.hit_rate:.1%}",
            "total_spent": f"${self.total_spent:.2f}",
            "total_won": f"${self.total_won:.2f}",
            "net_pnl": f"${self.net_pnl:+.2f}",
            "avg_multiplier_on_wins": f"{self.avg_multiplier_on_wins:.1f}x",
            "best_multiplier": f"{self.best_multiplier:.1f}x",
            "best_single_pnl": f"${self.best_single_pnl:+.2f}",
            "daily_spend": f"${self.daily_spend:.2f}",
            "daily_limit": f"${getattr(Config, 'PANIC_MAX_DAILY_SPEND', 50.0):.2f}",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# PANIC REVERSAL SCANNER
# ═══════════════════════════════════════════════════════════════════════════════

class PanicReversalScanner:
    """Scan active markets for extreme prices and generate reversal signals.

    The core insight: in 5-minute binary markets, when one side trades
    below $0.10, it implies <10% probability. But in volatile conditions,
    BTC can easily snap back within 5 minutes. The market is overpricing
    the panic and underpricing the reversal.

    We buy the panic side for pennies and wait. Most bets expire worthless.
    The few that hit pay 10-33x. Net positive over a series.
    """

    def __init__(
        self,
        bayesian_model: Optional["BayesianModel"] = None,
    ):
        self.bayesian_model = bayesian_model
        self.stats = PanicReversalStats()
        self.active_positions: list[PanicPosition] = []

        # Cooldown: don't re-enter the same market window
        self._entered_markets: set[str] = set()  # slug keys
        self._last_scan_time: float = 0.0

        print("[PANIC] 🎰 Panic reversal scanner initialized")

    # ── Scanning ──────────────────────────────────────────────────────────

    def scan(self, markets: list["Market"]) -> list[ReversalSignal]:
        """Scan markets for extreme price opportunities.

        Returns list of ReversalSignals for markets where one side
        is trading below the configured threshold.
        """
        self.stats.reset_daily_if_needed()
        signals: list[ReversalSignal] = []

        max_entry = getattr(Config, "PANIC_MAX_ENTRY_PRICE", 0.10)
        min_time = getattr(Config, "PANIC_MIN_TIME_LEFT", 60)
        max_concurrent = getattr(Config, "PANIC_MAX_CONCURRENT", 3)
        max_daily = getattr(Config, "PANIC_MAX_DAILY_SPEND", 50.0)
        bet_size = getattr(Config, "PANIC_BET_SIZE", 3.0)

        # Check budget constraints
        active_count = len([p for p in self.active_positions if not p.settled])
        if active_count >= max_concurrent:
            return signals

        if self.stats.daily_spend + bet_size > max_daily:
            return signals

        for market in markets:
            if market.closed or not market.accepting_orders:
                continue

            # Skip if already entered this market
            if market.slug in self._entered_markets:
                continue

            # Check time remaining
            time_left = market.seconds_until_close
            if time_left < min_time:
                continue

            # Check for extreme prices on either side
            for side, price in [("up", market.up_price), ("down", market.down_price)]:
                if price >= max_entry or price <= 0.005:
                    continue  # Not cheap enough, or basically zero (no liquidity)

                # Volatility filter: only trade in high/extreme vol
                vol_regime = "normal"
                mean_reversion_score = 0.5
                if self.bayesian_model:
                    asset = market.market_type.asset
                    vol_regime = self.bayesian_model.get_volatility_regime(asset)
                    # Get mean reversion probability from Bayesian model
                    p_up = self.bayesian_model.get_bayesian_probability(asset)
                    if side == "up":
                        # Cheap UP side = market thinks it's going down
                        # Mean reversion score = P(actually going up)
                        mean_reversion_score = p_up
                    else:
                        # Cheap DOWN side = market thinks it's going up
                        # Mean reversion score = P(actually going down)
                        mean_reversion_score = 1.0 - p_up

                # Only trade in high or extreme vol (reversals need vol)
                if vol_regime not in ("high", "extreme", "normal"):
                    continue

                # Boost score in high/extreme vol
                vol_boost = 1.0
                if vol_regime == "high":
                    vol_boost = 1.3
                elif vol_regime == "extreme":
                    vol_boost = 1.5

                multiplier = 1.0 / price
                adjusted_score = min(1.0, mean_reversion_score * vol_boost)

                # Calculate recommended size (small fixed bets)
                size = bet_size

                # Slightly increase size for higher conviction signals
                if adjusted_score > 0.6 and multiplier > 15:
                    size = min(bet_size * 1.5, getattr(Config, "PANIC_BET_SIZE", 3.0) * 2)

                signal = ReversalSignal(
                    market=market,
                    cheap_side=side,
                    price=price,
                    potential_multiplier=multiplier,
                    time_left_seconds=time_left,
                    volatility_regime=vol_regime,
                    mean_reversion_score=adjusted_score,
                    recommended_size=size,
                )
                signals.append(signal)

        # Sort by potential multiplier (highest first)
        signals.sort(key=lambda s: s.potential_multiplier, reverse=True)

        # Limit to remaining concurrent slots
        remaining_slots = max_concurrent - active_count
        remaining_budget = max_daily - self.stats.daily_spend
        filtered = []
        budget_used = 0.0
        for sig in signals:
            if len(filtered) >= remaining_slots:
                break
            if budget_used + sig.recommended_size > remaining_budget:
                continue
            filtered.append(sig)
            budget_used += sig.recommended_size

        self._last_scan_time = time.time()
        return filtered

    # ── Position Management ───────────────────────────────────────────────

    def open_position(self, signal: ReversalSignal, amount: float) -> PanicPosition:
        """Record a new panic reversal position."""
        shares = amount / signal.price if signal.price > 0 else 0.0
        position = PanicPosition(
            market_slug=signal.market.slug,
            market_timestamp=signal.market.timestamp,
            market_type=signal.market.market_type,
            cheap_side=signal.cheap_side,
            entry_price=signal.price,
            amount_usd=amount,
            shares=shares,
            entry_time=time.time(),
            token_id=signal.token_id,
        )
        self.active_positions.append(position)
        self._entered_markets.add(signal.market.slug)
        self.stats.record_attempt(amount)
        return position

    def check_take_profit(self, position: PanicPosition, current_price: float) -> bool:
        """Check if position should take profit.

        Returns True if current price exceeds take-profit multiplier × entry.
        """
        if position.settled:
            return False

        position.current_price = current_price
        tp_multiplier = getattr(Config, "PANIC_TAKE_PROFIT_MULTIPLIER", 3.0)

        if current_price >= position.entry_price * tp_multiplier:
            return True
        return False

    def settle_position(
        self,
        position: PanicPosition,
        outcome: str,
        exit_reason: str = "settlement",
    ):
        """Settle a panic reversal position.

        Args:
            position: The position to settle
            outcome: "up" or "down" — the market resolution
            exit_reason: "settlement", "take_profit", "expired"
        """
        if position.settled:
            return

        position.settled = True
        position.exit_reason = exit_reason

        if exit_reason == "take_profit":
            # Sold early at profit
            position.won = True
            sell_value = position.current_price * position.shares
            position.pnl = sell_value - position.amount_usd
            multiplier = position.current_price / position.entry_price
        elif outcome == position.cheap_side:
            # We bet on the cheap side and it won!
            position.won = True
            payout = position.shares * 1.0  # Binary: $1 per share
            position.pnl = payout - position.amount_usd
            multiplier = 1.0 / position.entry_price
        else:
            # We lost — shares expire worthless
            position.won = False
            position.pnl = -position.amount_usd
            multiplier = 0.0

        self.stats.record_settlement(position.won, position.pnl, multiplier)

        emoji = "🎯" if position.won else "💀"
        mult_str = f"{multiplier:.1f}x" if position.won else "0x"
        print(f"[PANIC] {emoji} {position.market_slug}: "
              f"{'WON' if position.won else 'LOST'} | "
              f"Entry: ${position.entry_price:.3f} | "
              f"PnL: ${position.pnl:+.2f} | "
              f"Mult: {mult_str} | "
              f"Reason: {exit_reason}")

    # ── Cleanup ───────────────────────────────────────────────────────────

    def cleanup_settled(self):
        """Remove settled positions older than 10 minutes."""
        cutoff = time.time() - 600
        self.active_positions = [
            p for p in self.active_positions
            if not p.settled or p.entry_time > cutoff
        ]

    def get_active_positions(self) -> list[PanicPosition]:
        """Get currently active (unsettled) positions."""
        return [p for p in self.active_positions if not p.settled]

    # ── Stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """Get strategy statistics for dashboard."""
        active = self.get_active_positions()
        unrealized = sum(p.unrealized_pnl for p in active)

        return {
            **self.stats.to_dict(),
            "active_positions": len(active),
            "unrealized_pnl": f"${unrealized:+.2f}",
            "positions": [
                {
                    "market": p.market_slug,
                    "side": p.cheap_side,
                    "entry": f"${p.entry_price:.3f}",
                    "current": f"${p.current_price:.3f}" if p.current_price > 0 else "—",
                    "multiplier": f"{p.current_multiplier:.1f}x" if p.current_multiplier > 0 else "—",
                    "amount": f"${p.amount_usd:.2f}",
                    "pnl": f"${p.unrealized_pnl:+.2f}" if not p.settled else f"${p.pnl:+.2f}",
                    "status": "active" if not p.settled else ("won" if p.won else "lost"),
                }
                for p in active
            ],
        }
