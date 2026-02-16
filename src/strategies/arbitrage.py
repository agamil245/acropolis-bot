"""
Micro-arbitrage strategy - THE PRIMARY MONEY MAKER.

This strategy exploits structural pricing inefficiencies when YES + NO < $1.
When markets are mispriced, we buy the underpriced side with zero directional risk.

Example:
- YES price: $0.48
- NO price: $0.48
- Combined: $0.96 (< $1.00)
- Edge: 4% risk-free profit

We buy the cheaper side (or both if equally underpriced), wait for resolution,
and collect $1.00 per share regardless of outcome.
"""

import time
from dataclasses import dataclass
from typing import Optional

from src.config import Config
from src.core.polymarket import Market, MarketType


@dataclass
class ArbitrageSignal:
    """Signal for an arbitrage opportunity."""
    
    market: Market
    direction: str  # "up", "down", or "both"
    edge_pct: float  # arbitrage edge in percentage
    recommended_size: float  # suggested bet size in USD
    up_price: float
    down_price: float
    combined_price: float
    reason: str
    confidence: float = 1.0  # arbitrage is risk-free (no directional prediction)
    timestamp: int = 0

    def __post_init__(self):
        self.timestamp = int(time.time() * 1000)


class ArbitrageStrategy:
    """
    Micro-arbitrage strategy implementation.
    
    This is pure structural edge - no prediction needed.
    When YES + NO < threshold (default 0.98), we have guaranteed profit.
    """

    def __init__(self):
        self.threshold = Config.ARB_THRESHOLD
        self.min_edge_pct = Config.ARB_MIN_EDGE_PCT
        self.min_bet = Config.ARB_MIN_BET
        self.max_exposure = Config.ARB_MAX_EXPOSURE
        self.current_exposure = 0.0
        
        # Track opportunities per market to avoid spam
        self._last_signal_time: dict[str, float] = {}
        self._signal_cooldown = 1.0  # seconds between signals for same market

    def evaluate(self, market: Market, bankroll: float) -> Optional[ArbitrageSignal]:
        """
        Evaluate a market for arbitrage opportunities.
        
        Args:
            market: The market to evaluate
            bankroll: Current available bankroll
            
        Returns:
            ArbitrageSignal if opportunity found, None otherwise
        """
        # Skip if market is closed or not accepting orders
        if market.closed or not market.accepting_orders:
            return None

        # Calculate combined price and edge
        combined = market.up_price + market.down_price
        edge_pct = (1.0 - combined) * 100

        # Check if edge meets minimum threshold
        if combined >= self.threshold or edge_pct < self.min_edge_pct:
            return None

        # Check cooldown (avoid spamming same market)
        now = time.time()
        last_signal = self._last_signal_time.get(market.slug, 0)
        if now - last_signal < self._signal_cooldown:
            return None

        # Check exposure limits
        if self.current_exposure >= self.max_exposure:
            return None

        # Determine which side to buy
        # Buy the cheaper side (better value)
        if market.up_price < market.down_price:
            direction = "up"
            buy_price = market.up_price
        elif market.down_price < market.up_price:
            direction = "down"
            buy_price = market.down_price
        else:
            # Both equal - buy the one that brings combined closer to $1
            # Or just pick "up" as default
            direction = "up"
            buy_price = market.up_price

        # Calculate position size
        # For arbitrage, we want to maximize edge while respecting limits
        available_exposure = min(
            self.max_exposure - self.current_exposure,
            bankroll * Config.get_max_exposure_for_risk()
        )
        
        # Size based on edge and available exposure
        # Larger edge = larger position (within limits)
        size_multiplier = min(edge_pct / 2.0, 1.0)  # Cap at 1.0
        recommended_size = min(
            available_exposure * size_multiplier,
            Config.MAX_BET,
            bankroll * 0.10  # Never more than 10% of bankroll per trade
        )
        
        recommended_size = max(recommended_size, self.min_bet)

        # Build signal
        reason = (
            f"Arbitrage opportunity detected: {market.slug} | "
            f"YES: ${market.up_price:.4f} | NO: ${market.down_price:.4f} | "
            f"Combined: ${combined:.4f} (< ${self.threshold:.4f}) | "
            f"Edge: {edge_pct:.2f}% | "
            f"Buying: {direction.upper()} @ ${buy_price:.4f}"
        )

        signal = ArbitrageSignal(
            market=market,
            direction=direction,
            edge_pct=edge_pct,
            recommended_size=recommended_size,
            up_price=market.up_price,
            down_price=market.down_price,
            combined_price=combined,
            reason=reason,
            confidence=1.0  # Risk-free arbitrage
        )

        # Update cooldown
        self._last_signal_time[market.slug] = now

        return signal

    def evaluate_all_markets(
        self,
        markets: list[Market],
        bankroll: float
    ) -> list[ArbitrageSignal]:
        """
        Evaluate multiple markets for arbitrage opportunities.
        
        Returns list sorted by edge percentage (best first).
        """
        signals = []
        
        for market in markets:
            signal = self.evaluate(market, bankroll)
            if signal:
                signals.append(signal)

        # Sort by edge (best opportunities first)
        signals.sort(key=lambda s: s.edge_pct, reverse=True)
        
        return signals

    def update_exposure(self, amount: float):
        """Update current exposure (called when trade is placed)."""
        self.current_exposure += amount

    def release_exposure(self, amount: float):
        """Release exposure (called when trade settles)."""
        self.current_exposure = max(0, self.current_exposure - amount)

    def get_stats(self) -> dict:
        """Get strategy statistics."""
        return {
            "threshold": self.threshold,
            "min_edge_pct": self.min_edge_pct,
            "current_exposure": self.current_exposure,
            "max_exposure": self.max_exposure,
            "utilization_pct": (self.current_exposure / self.max_exposure * 100) if self.max_exposure > 0 else 0,
        }


def calculate_arbitrage_pnl(
    amount: float,
    entry_price: float,
    won: bool,
    fee_pct: float
) -> tuple[float, float, float]:
    """
    Calculate P&L for an arbitrage trade.
    
    Args:
        amount: Bet amount in USD
        entry_price: Entry price (what we paid per share)
        won: Whether we won (received $1 per share)
        fee_pct: Fee percentage as decimal (e.g., 0.025 = 2.5%)
        
    Returns:
        (gross_profit, fee_amount, net_profit)
    """
    shares = amount / entry_price if entry_price > 0 else 0

    if won:
        # Win: receive $1 per share
        gross_payout = shares
        gross_profit = gross_payout - amount
        fee_amount = gross_profit * fee_pct if gross_profit > 0 else 0
        net_profit = gross_profit - fee_amount
    else:
        # Loss: lose everything (shouldn't happen often in arbitrage)
        gross_profit = -amount
        fee_amount = 0
        net_profit = -amount

    return (gross_profit, fee_amount, net_profit)
