"""Trading strategies for AcropolisBot."""

from src.strategies.arbitrage import ArbitrageStrategy, ArbitrageSignal, calculate_arbitrage_pnl
from src.strategies.streak import (
    StreakSignal, detect_streak, evaluate, kelly_size,
    StreakAnalyzer, REVERSAL_RATES,
)
from src.strategies.copytrade import (
    CopySignal, CopytradeMonitor, TraderProfile, TraderScoreboard,
    SelectiveCopyFilter,
)

__all__ = [
    # Arbitrage
    "ArbitrageStrategy",
    "ArbitrageSignal",
    "calculate_arbitrage_pnl",
    # Streak
    "StreakSignal",
    "StreakAnalyzer",
    "detect_streak",
    "evaluate",
    "kelly_size",
    "REVERSAL_RATES",
    # Copytrade
    "CopySignal",
    "CopytradeMonitor",
    "TraderProfile",
    "TraderScoreboard",
    "SelectiveCopyFilter",
]
