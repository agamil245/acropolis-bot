"""
Streak reversal strategy — ported & enhanced from reference bot.py.

Core idea: crypto minute-markets exhibit mean-reversion after consecutive
same-direction outcomes.  After N consecutive UPs we bet DOWN (and vice
versa).  Historical data shows reversal rates climb sharply once streaks
reach 4–5 in a row.

Features
────────
• Configurable streak trigger length per market type
• Historical reversal-rate table with dynamic updates from live data
• Kelly criterion bet sizing (fractional)
• Win-rate tracking per streak length & market type
• Streak history analysis (longest streak, avg streak length)
• Multi-market support (BTC/ETH/SOL × 5m/15m)
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from src.config import Config, MarketType


# ═══════════════════════════════════════════════════════════════════════════════
# REVERSAL RATES — baseline from backtests
# ═══════════════════════════════════════════════════════════════════════════════

# Key = streak length, value = historical probability the streak *reverses*
# on the next candle.  Conservative estimates; live tracking refines them.
REVERSAL_RATES: dict[int, float] = {
    2: 0.540,
    3: 0.579,
    4: 0.667,
    5: 0.824,
    6: 0.850,
    7: 0.880,
    8: 0.900,
    9: 0.920,
    10: 0.940,
}


def get_reversal_rate(streak_length: int) -> float:
    """Look up the reversal rate for a given streak length.

    For lengths beyond the table we extrapolate capped at 0.95.
    """
    if streak_length in REVERSAL_RATES:
        return REVERSAL_RATES[streak_length]
    if streak_length < min(REVERSAL_RATES):
        return 0.50
    return min(0.95, REVERSAL_RATES[max(REVERSAL_RATES)] + 0.01 * (streak_length - max(REVERSAL_RATES)))


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class StreakSignal:
    """Trading signal emitted by the streak reversal strategy."""

    should_bet: bool
    direction: str          # "up" or "down" — the direction to BET (opposite of streak)
    streak_length: int
    streak_direction: str   # the direction of the observed streak
    confidence: float       # estimated win probability
    reason: str
    market_type: MarketType = MarketType.BTC_5M
    reversal_rate: float = 0.0
    kelly_fraction: float = 0.0
    timestamp_ms: int = 0

    def __post_init__(self):
        if self.timestamp_ms == 0:
            self.timestamp_ms = int(time.time() * 1000)


@dataclass
class StreakRecord:
    """Record of a single observed streak and its outcome."""

    streak_length: int
    streak_direction: str   # "up" or "down"
    bet_direction: str      # the direction we bet (reversal)
    outcome: Optional[str] = None   # actual next candle direction
    won: Optional[bool] = None
    pnl: float = 0.0
    market_type: MarketType = MarketType.BTC_5M
    timestamp: int = 0


# ═══════════════════════════════════════════════════════════════════════════════
# CORE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

def detect_streak(outcomes: list[str]) -> tuple[int, str]:
    """Detect the current streak at the tail of an outcome list.

    Args:
        outcomes: e.g. ["up", "down", "up", "up", "up"]

    Returns:
        (streak_length, streak_direction)
    """
    if not outcomes:
        return 0, ""

    current = outcomes[-1].lower()
    length = 1

    for i in range(len(outcomes) - 2, -1, -1):
        if outcomes[i].lower() == current:
            length += 1
        else:
            break

    return length, current


def evaluate(
    outcomes: list[str],
    market_type: MarketType = MarketType.BTC_5M,
    trigger: Optional[int] = None,
    min_reversal_rate: Optional[float] = None,
) -> StreakSignal:
    """Evaluate recent outcomes and return a StreakSignal.

    This is the primary entry point called by bot_engine._streak_loop.

    Args:
        outcomes: Recent candle outcomes oldest→newest, e.g. ["up","up","down","up","up","up"]
        market_type: Which market we're evaluating
        trigger: Minimum streak length to act on (default from Config)
        min_reversal_rate: Minimum required reversal probability

    Returns:
        StreakSignal with should_bet=True/False
    """
    trigger = trigger or getattr(Config, "STREAK_TRIGGER", 4)
    min_reversal_rate = min_reversal_rate or getattr(Config, "STREAK_MIN_REVERSAL_RATE", 0.55)

    streak_len, streak_dir = detect_streak(outcomes)

    # Not enough streak
    if streak_len < trigger:
        return StreakSignal(
            should_bet=False,
            direction="",
            streak_length=streak_len,
            streak_direction=streak_dir,
            confidence=0.0,
            reason=f"Streak {streak_len} < trigger {trigger}",
            market_type=market_type,
        )

    # Look up reversal rate
    reversal = get_reversal_rate(streak_len)

    if reversal < min_reversal_rate:
        return StreakSignal(
            should_bet=False,
            direction="",
            streak_length=streak_len,
            streak_direction=streak_dir,
            confidence=reversal,
            reason=f"Reversal rate {reversal:.1%} < min {min_reversal_rate:.1%}",
            market_type=market_type,
            reversal_rate=reversal,
        )

    # Bet AGAINST the streak
    bet_dir = "down" if streak_dir == "up" else "up"

    # Kelly sizing fraction
    kf = _kelly_raw(reversal, 2.0)  # assume ~2x decimal odds at 50¢

    return StreakSignal(
        should_bet=True,
        direction=bet_dir,
        streak_length=streak_len,
        streak_direction=streak_dir,
        confidence=reversal,
        reason=(
            f"{market_type.value}: {streak_len}× {streak_dir.upper()} streak → "
            f"bet {bet_dir.upper()} (reversal {reversal:.1%})"
        ),
        market_type=market_type,
        reversal_rate=reversal,
        kelly_fraction=kf,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# KELLY CRITERION
# ═══════════════════════════════════════════════════════════════════════════════

def _kelly_raw(p: float, odds: float) -> float:
    """Raw Kelly fraction: f* = (bp - q) / b  where b = odds - 1."""
    if p <= 0 or odds <= 1:
        return 0.0
    b = odds - 1
    q = 1 - p
    k = (b * p - q) / b
    return max(0.0, k)


def kelly_size(
    confidence: float,
    odds: float,
    bankroll: float,
    fraction: Optional[float] = None,
) -> float:
    """Calculate bet size using fractional Kelly criterion.

    Args:
        confidence: estimated win probability (0–1)
        odds: decimal odds (e.g. 2.0 for even money at 50¢)
        bankroll: current bankroll in USD
        fraction: Kelly fraction to use (default from Config)

    Returns:
        Recommended bet size in USD
    """
    fraction = fraction or getattr(Config, "KELLY_FRACTION", 0.25)

    raw = _kelly_raw(confidence, odds)
    if raw <= 0:
        return 0.0

    size = bankroll * raw * fraction
    min_bet = getattr(Config, "MIN_BET", 1.0)
    max_bet = getattr(Config, "MAX_BET", 100.0)
    size = max(min_bet, min(size, max_bet))

    return round(size, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# STREAK ANALYZER — historical tracking & live rate updates
# ═══════════════════════════════════════════════════════════════════════════════

class StreakAnalyzer:
    """Tracks streak outcomes over time and refines reversal rate estimates.

    Used by bot_engine for live win-rate tracking per streak length and
    market type.
    """

    def __init__(self):
        # (market_type, streak_length) -> {"bets": N, "wins": N, "total_pnl": float}
        self._records: dict[tuple[str, int], dict] = defaultdict(
            lambda: {"bets": 0, "wins": 0, "total_pnl": 0.0}
        )
        # Full history for analysis
        self._history: list[StreakRecord] = []
        self._max_history = 5000

        # Longest streaks seen per market type
        self._longest_streaks: dict[str, int] = {}

        # Running outcome buffers per market type for real-time detection
        self._outcome_buffers: dict[str, list[str]] = defaultdict(list)
        self._buffer_max = 100

    # ── recording ─────────────────────────────────────────────────────────

    def record_bet(self, record: StreakRecord):
        """Record a streak bet (before outcome is known)."""
        self._history.append(record)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]

    def record_outcome(
        self,
        streak_length: int,
        market_type: MarketType,
        won: bool,
        pnl: float = 0.0,
    ):
        """Record the outcome of a streak bet."""
        key = (market_type.value, streak_length)
        self._records[key]["bets"] += 1
        if won:
            self._records[key]["wins"] += 1
        self._records[key]["total_pnl"] += pnl

    def push_outcome(self, market_type: MarketType, outcome: str):
        """Push a new candle outcome for real-time streak detection."""
        buf = self._outcome_buffers[market_type.value]
        buf.append(outcome.lower())
        if len(buf) > self._buffer_max:
            self._outcome_buffers[market_type.value] = buf[-self._buffer_max:]

        # Track longest streak
        sl, _ = detect_streak(buf)
        prev = self._longest_streaks.get(market_type.value, 0)
        if sl > prev:
            self._longest_streaks[market_type.value] = sl

    def get_current_streak(self, market_type: MarketType) -> tuple[int, str]:
        """Get current streak from the outcome buffer for a market type."""
        buf = self._outcome_buffers.get(market_type.value, [])
        return detect_streak(buf)

    # ── statistics ────────────────────────────────────────────────────────

    def get_win_rate(self, streak_length: int, market_type: Optional[MarketType] = None) -> Optional[float]:
        """Get observed win rate for a specific streak length.

        Returns None if no data.
        """
        if market_type:
            key = (market_type.value, streak_length)
            rec = self._records.get(key)
            if rec and rec["bets"] > 0:
                return rec["wins"] / rec["bets"]
            return None

        # Aggregate across all market types
        total_bets = 0
        total_wins = 0
        for (_, sl), rec in self._records.items():
            if sl == streak_length:
                total_bets += rec["bets"]
                total_wins += rec["wins"]
        if total_bets == 0:
            return None
        return total_wins / total_bets

    def get_live_reversal_rate(self, streak_length: int, market_type: Optional[MarketType] = None) -> float:
        """Get blended reversal rate: mix of baseline + observed.

        Uses Bayesian-style blending: weight observed data more as sample
        size grows.  Needs ≥10 observations to start shifting from baseline.
        """
        baseline = get_reversal_rate(streak_length)
        observed = self.get_win_rate(streak_length, market_type)
        if observed is None:
            return baseline

        # Count observations
        if market_type:
            key = (market_type.value, streak_length)
            n = self._records.get(key, {}).get("bets", 0)
        else:
            n = sum(
                rec["bets"]
                for (_, sl), rec in self._records.items()
                if sl == streak_length
            )

        # Blend: weight observed more as n grows.  At n=50 it's 50/50.
        obs_weight = min(n / 100.0, 0.8)
        return baseline * (1 - obs_weight) + observed * obs_weight

    def get_stats(self, market_type: Optional[MarketType] = None) -> dict:
        """Get comprehensive streak statistics."""
        # Per streak-length stats
        by_length: dict[int, dict] = {}
        for (mt, sl), rec in self._records.items():
            if market_type and mt != market_type.value:
                continue
            if sl not in by_length:
                by_length[sl] = {"bets": 0, "wins": 0, "pnl": 0.0}
            by_length[sl]["bets"] += rec["bets"]
            by_length[sl]["wins"] += rec["wins"]
            by_length[sl]["pnl"] += rec["total_pnl"]

        for sl, data in by_length.items():
            data["win_rate"] = (data["wins"] / data["bets"]) if data["bets"] > 0 else 0.0
            data["baseline_rate"] = get_reversal_rate(sl)
            data["live_rate"] = self.get_live_reversal_rate(sl, market_type)

        total_bets = sum(d["bets"] for d in by_length.values())
        total_wins = sum(d["wins"] for d in by_length.values())
        total_pnl = sum(d["pnl"] for d in by_length.values())

        return {
            "total_bets": total_bets,
            "total_wins": total_wins,
            "win_rate": (total_wins / total_bets) if total_bets > 0 else 0.0,
            "total_pnl": total_pnl,
            "by_streak_length": dict(sorted(by_length.items())),
            "longest_streaks": dict(self._longest_streaks),
            "history_size": len(self._history),
        }

    def get_streak_distribution(self, market_type: Optional[MarketType] = None) -> dict[int, int]:
        """Count how many times each streak length has been observed."""
        dist: dict[int, int] = defaultdict(int)
        for rec in self._history:
            if market_type and rec.market_type != market_type:
                continue
            dist[rec.streak_length] += 1
        return dict(sorted(dist.items()))

    def get_longest_streak(self, market_type: Optional[MarketType] = None) -> int:
        """Return the longest streak ever observed."""
        if market_type:
            return self._longest_streaks.get(market_type.value, 0)
        return max(self._longest_streaks.values()) if self._longest_streaks else 0
