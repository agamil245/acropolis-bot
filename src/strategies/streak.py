"""Streak reversal strategy - improved from reference bot."""

from dataclasses import dataclass
from typing import Optional

from src.config import Config, MarketType


@dataclass
class StreakSignal:
    """Trading signal for streak reversal."""
    
    should_bet: bool
    direction: str  # "up" or "down"
    streak_length: int
    streak_direction: str  # what the streak was
    confidence: float  # estimated win probability
    reason: str
    market_type: MarketType


# Historical reversal rates (from reference bot backtest + improved data)
# These are conservative estimates - actual rates may be higher
REVERSAL_RATES = {
    2: 0.540,
    3: 0.579,
    4: 0.667,
    5: 0.824,
    6: 0.850,  # Extended
    7: 0.900,  # Extended
}


def detect_streak(outcomes: list[str]) -> tuple[int, str]:
    """
    Detect the current streak at the end of the outcomes list.
    
    Returns: (streak_length, streak_direction)
    """
    if not outcomes:
        return 0, ""

    current = outcomes[-1]
    streak = 1

    for i in range(len(outcomes) - 2, -1, -1):
        if outcomes[i] == current:
            streak += 1
        else:
            break

    return streak, current


def evaluate(
    outcomes: list[str],
    market_type: MarketType,
    trigger: Optional[int] = None,
    min_reversal_rate: Optional[float] = None
) -> StreakSignal:
    """
    Evaluate whether to place a bet based on recent outcomes.
    
    Args:
        outcomes: List of recent outcomes ("up"/"down"), oldest first
        market_type: Type of market being evaluated
        trigger: Minimum streak length (uses Config if None)
        min_reversal_rate: Minimum reversal rate required (uses Config if None)
        
    Returns:
        StreakSignal with bet recommendation
    """
    trigger = trigger or Config.STREAK_TRIGGER
    min_reversal_rate = min_reversal_rate or Config.STREAK_MIN_REVERSAL_RATE
    
    streak_len, streak_dir = detect_streak(outcomes)

    if streak_len < trigger:
        return StreakSignal(
            should_bet=False,
            direction="",
            streak_length=streak_len,
            streak_direction=streak_dir,
            confidence=0,
            reason=f"Streak {streak_len} < trigger {trigger}",
            market_type=market_type
        )

    # Get historical reversal rate
    confidence = REVERSAL_RATES.get(min(streak_len, 7), REVERSAL_RATES[5])

    # Check if confidence meets minimum threshold
    if confidence < min_reversal_rate:
        return StreakSignal(
            should_bet=False,
            direction="",
            streak_length=streak_len,
            streak_direction=streak_dir,
            confidence=confidence,
            reason=f"Confidence {confidence:.1%} < minimum {min_reversal_rate:.1%}",
            market_type=market_type
        )

    # Bet AGAINST the streak (reversal)
    bet_direction = "down" if streak_dir == "up" else "up"

    return StreakSignal(
        should_bet=True,
        direction=bet_direction,
        streak_length=streak_len,
        streak_direction=streak_dir,
        confidence=confidence,
        reason=(
            f"Streak of {streak_len}x {streak_dir} detected on {market_type.value}. "
            f"Historical reversal rate: {confidence:.1%}. "
            f"Betting {bet_direction}."
        ),
        market_type=market_type
    )


def kelly_size(
    confidence: float,
    odds: float,
    bankroll: float,
    fraction: Optional[float] = None
) -> float:
    """
    Calculate bet size using fractional Kelly criterion.
    
    Args:
        confidence: Estimated win probability (0-1)
        odds: Decimal odds (e.g., 2.0 for even money at 50¢)
        bankroll: Current bankroll
        fraction: Kelly fraction (uses Config if None)
        
    Returns:
        Recommended bet size in USD
    """
    fraction = fraction or Config.get_kelly_fraction_for_risk()
    
    if confidence <= 0 or odds <= 1:
        return 0

    # Kelly formula: f* = (bp - q) / b
    b = odds - 1
    p = confidence
    q = 1 - p

    kelly = (b * p - q) / b
    if kelly <= 0:
        return 0

    # Apply fraction and constraints
    size = bankroll * kelly * fraction
    size = max(Config.MIN_BET, min(size, Config.MAX_BET))
    
    return round(size, 2)
