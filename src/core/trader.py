"""Trading execution engine with Kelly criterion, risk management,
P&L tracking, and both paper and live trading modes.

Production-ready trading engine with:
- Full Kelly criterion bet sizing with fractional Kelly support
- Position management with exposure tracking
- Risk management (drawdown protection, circuit breakers, max exposure)
- Per-strategy P&L tracking
- Bankroll management with auto-compounding
- Trade execution with slippage protection
- Comprehensive trade history with nested JSON persistence
"""

import json
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

from src.config import Config, LOCAL_TZ, TIMEZONE_NAME, MarketType, MARKET_PROFILES
from src.core.polymarket import Market, PolymarketClient, MarketDataCache

if TYPE_CHECKING:
    pass


# ═══════════════════════════════════════════════════════════════════════════════
# KELLY CRITERION BET SIZING
# ═══════════════════════════════════════════════════════════════════════════════

def kelly_criterion(win_prob: float, odds: float, fraction: float = 0.25) -> float:
    """Calculate Kelly criterion bet size as fraction of bankroll.

    Full Kelly formula: f* = (bp - q) / b
    Where:
        b = net odds (decimal odds - 1)
        p = probability of winning
        q = probability of losing (1 - p)
        f* = fraction of bankroll to bet

    We use fractional Kelly (typically 1/4) for safety.

    Args:
        win_prob: Estimated probability of winning (0-1)
        odds: Decimal odds (e.g., 2.0 means 2:1 payout)
        fraction: Kelly fraction (0.25 = quarter Kelly)

    Returns:
        Optimal bet size as fraction of bankroll (0 to fraction)
    """
    if win_prob <= 0 or win_prob >= 1 or odds <= 1:
        return 0.0

    b = odds - 1  # Net odds
    p = win_prob
    q = 1 - p

    # Kelly formula
    kelly_f = (b * p - q) / b

    # Negative Kelly means don't bet (negative edge)
    if kelly_f <= 0:
        return 0.0

    # Apply fractional Kelly for safety
    return kelly_f * fraction


def kelly_size(
    win_prob: float,
    odds: float,
    bankroll: float,
    fraction: Optional[float] = None,
    min_bet: Optional[float] = None,
    max_bet: Optional[float] = None,
) -> float:
    """Calculate Kelly criterion bet amount in USD.

    Combines Kelly fraction with bankroll constraints.

    Args:
        win_prob: Estimated probability of winning
        odds: Decimal odds
        bankroll: Current bankroll
        fraction: Kelly fraction override (None = use config)
        min_bet: Minimum bet override
        max_bet: Maximum bet override

    Returns:
        Bet amount in USD, clamped to [min_bet, max_bet]
    """
    if fraction is None:
        fraction = Config.get_kelly_fraction_for_risk()
    if min_bet is None:
        min_bet = Config.MIN_BET
    if max_bet is None:
        max_bet = Config.MAX_BET

    # Kelly fraction of bankroll
    kelly_frac = kelly_criterion(win_prob, odds, fraction)
    kelly_amount = kelly_frac * bankroll

    # Also cap at max exposure percentage of bankroll
    max_exposure = bankroll * Config.get_max_exposure_for_risk()

    # Apply all constraints
    amount = min(kelly_amount, max_exposure, max_bet, bankroll)
    amount = max(min_bet, amount)

    # Don't bet more than bankroll
    amount = min(amount, bankroll)

    return round(amount, 2)


def fixed_bet_size(bankroll: float) -> float:
    """Fixed bet sizing (non-Kelly mode).

    If AUTO_COMPOUND is enabled, scales bet size with bankroll.
    """
    if Config.AUTO_COMPOUND:
        # Scale with bankroll: bet_amount * (bankroll / initial_bankroll)
        scale = bankroll / Config.INITIAL_BANKROLL
        amount = Config.BET_AMOUNT * scale
    else:
        amount = Config.BET_AMOUNT

    # Clamp
    amount = max(Config.MIN_BET, min(amount, Config.MAX_BET, bankroll))
    return round(amount, 2)


# ═══════════════════════════════════════════════════════════════════════════════
# TRADE MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """Record of a trade with full execution details, settlement, and analysis.

    Supports both paper and live trades with comprehensive tracking for
    backtesting, analytics, and P&L reporting.
    """

    # === IDENTIFICATION ===
    id: str
    timestamp: int  # Market window timestamp
    market_type: MarketType
    market_slug: str
    direction: str  # "up" or "down"
    strategy: str  # "arbitrage", "streak", "copytrade"

    # === EXECUTION ===
    amount: float  # Actual filled amount in USD
    requested_amount: float  # Originally requested amount
    entry_price: float  # Displayed market price at decision time
    execution_price: float  # Actual fill price after slippage
    shares: float  # Number of shares purchased
    executed_at: int  # Unix timestamp in milliseconds

    # === FEES & COSTS ===
    fee_rate_bps: int = 0  # Base fee in basis points
    fee_pct: float = 0.0  # Actual fee percentage at execution price
    fee_amount: float = 0.0  # Fee in USD (calculated on settlement)
    slippage_pct: float = 0.0  # Slippage from walking the book
    spread: float = 0.0  # Bid-ask spread at entry
    fill_pct: float = 100.0  # Percentage of order filled
    delay_impact_pct: float = 0.0  # Price impact from copy delay

    # === SETTLEMENT ===
    outcome: Optional[str] = None  # "up" or "down" after resolution
    won: Optional[bool] = None
    settled_at: Optional[int] = None  # Unix ms
    gross_pnl: float = 0.0  # Gross profit before fees
    net_pnl: float = 0.0  # Net profit after fees
    gross_payout: float = 0.0  # Total payout ($1/share if won)
    settlement_status: str = "pending"  # "pending", "settled", "force_exit"
    force_exit_reason: Optional[str] = None

    # === STRATEGY-SPECIFIC ===
    streak_length: int = 0
    confidence: float = 0.0
    arbitrage_edge: float = 0.0

    # === COPYTRADE ===
    copied_from: Optional[str] = None  # Wallet address
    trader_name: Optional[str] = None
    trader_direction: Optional[str] = None
    trader_amount: Optional[float] = None
    trader_price: Optional[float] = None
    trader_timestamp: Optional[int] = None
    copy_delay_ms: Optional[int] = None
    delay_model_breakdown: Optional[dict] = None

    # === RISK METRICS ===
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0
    peak_bankroll: float = 0.0

    # === MARKET CONTEXT ===
    market_volume: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    price_at_signal: float = 0.0
    price_movement_pct: float = 0.0
    opposite_price: Optional[float] = None
    market_bias: Optional[str] = None  # "bullish", "bearish", "neutral"

    # === TIMING ===
    hour_utc: Optional[int] = None
    minute_of_hour: Optional[int] = None
    day_of_week: Optional[int] = None
    seconds_into_window: Optional[int] = None
    window_close_time: Optional[int] = None
    resolution_time: Optional[int] = None
    resolution_delay_seconds: Optional[float] = None

    # === SESSION TRACKING ===
    session_trade_number: Optional[int] = None
    session_wins_before: Optional[int] = None
    session_losses_before: Optional[int] = None
    session_pnl_before: Optional[float] = None
    consecutive_wins: int = 0
    consecutive_losses: int = 0

    # === MODE ===
    paper: bool = True
    order_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d['market_type'] = self.market_type.value
        return d

    def to_nested_json(self) -> dict:
        """Convert trade to nested JSON structure for organized storage."""
        trade_id = self.id

        market = {
            "timestamp": self.timestamp,
            "slug": self.market_slug,
            "type": self.market_type.value,
            "window_close": self.window_close_time or (self.timestamp + self.market_type.interval_seconds),
            "volume": self.market_volume,
        }

        position = {
            "direction": self.direction,
            "amount": self.amount,
            "requested_amount": self.requested_amount,
            "shares": self.shares,
        }

        execution = {
            "timestamp": self.executed_at,
            "entry_price": self.entry_price,
            "fill_price": self.execution_price,
            "spread": self.spread,
            "slippage_pct": self.slippage_pct,
            "fill_pct": self.fill_pct,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "price_movement_pct": self.price_movement_pct,
        }

        fees = {
            "rate_bps": self.fee_rate_bps,
            "pct": self.fee_pct,
            "amount": self.fee_amount,
        }

        copytrade = None
        if self.strategy == "copytrade" and self.copied_from:
            copytrade = {
                "wallet": self.copied_from,
                "name": self.trader_name,
                "direction": self.trader_direction,
                "amount": self.trader_amount,
                "price": self.trader_price,
                "timestamp": self.trader_timestamp,
                "delay_ms": self.copy_delay_ms,
                "delay_impact_pct": self.delay_impact_pct,
                "delay_breakdown": self.delay_model_breakdown,
            }

        settlement = {
            "status": self.settlement_status,
            "outcome": self.outcome,
            "won": self.won,
            "timestamp": self.settled_at,
            "resolution_delay_sec": self.resolution_delay_seconds,
            "gross_payout": self.gross_payout,
            "gross_profit": self.gross_pnl,
            "fee_amount": self.fee_amount,
            "net_profit": self.net_pnl,
        }
        if self.settlement_status == "force_exit":
            settlement["force_exit_reason"] = self.force_exit_reason

        context = {
            "strategy": self.strategy,
            "mode": "paper" if self.paper else "live",
            "market_bias": self.market_bias or "neutral",
            "confidence": self.confidence,
            "streak_length": self.streak_length,
            "arbitrage_edge": self.arbitrage_edge,
        }

        risk = {
            "bankroll_before": self.bankroll_before,
            "bankroll_after": self.bankroll_after,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
        }

        session = {
            "trade_number": self.session_trade_number or 1,
            "wins_before": self.session_wins_before or 0,
            "losses_before": self.session_losses_before or 0,
            "pnl_before": self.session_pnl_before or 0.0,
        }

        timing = {
            "hour_utc": self.hour_utc,
            "minute": self.minute_of_hour,
            "day_of_week": self.day_of_week,
            "seconds_into_window": self.seconds_into_window,
        }

        result = {
            "id": trade_id,
            "market": market,
            "position": position,
            "execution": execution,
            "fees": fees,
        }
        if copytrade:
            result["copytrade"] = copytrade
        result["settlement"] = settlement
        result["context"] = context
        result["risk"] = risk
        result["session"] = session
        result["timing"] = timing

        return result

    @classmethod
    def from_dict(cls, data: dict) -> 'Trade':
        """Create Trade from flat dict."""
        data = data.copy()
        if 'market_type' in data:
            data['market_type'] = MarketType(data['market_type'])
        return cls(**data)

    @classmethod
    def from_nested_json(cls, data: dict) -> 'Trade':
        """Create Trade from nested JSON structure."""
        market = data.get("market", {})
        position = data.get("position", {})
        execution = data.get("execution", {})
        fees = data.get("fees", {})
        copytrade = data.get("copytrade")
        settlement = data.get("settlement", {})
        context = data.get("context", {})
        risk = data.get("risk", {})
        session = data.get("session", {})
        timing = data.get("timing", {})

        # Parse market type
        market_type_str = market.get("type", "btc-updown-5m")
        try:
            market_type = MarketType(market_type_str)
        except ValueError:
            market_type = MarketType.BTC_5M

        return cls(
            id=data.get("id", ""),
            timestamp=market.get("timestamp", 0),
            market_type=market_type,
            market_slug=market.get("slug", ""),
            direction=position.get("direction", ""),
            strategy=context.get("strategy", "streak"),
            amount=position.get("amount", 0.0),
            requested_amount=position.get("requested_amount", position.get("amount", 0.0)),
            entry_price=execution.get("entry_price", 0.5),
            execution_price=execution.get("fill_price", execution.get("entry_price", 0.5)),
            shares=position.get("shares", 0.0),
            executed_at=execution.get("timestamp", 0),
            fee_rate_bps=fees.get("rate_bps", 0),
            fee_pct=fees.get("pct", 0.0),
            fee_amount=fees.get("amount", 0.0) or settlement.get("fee_amount", 0.0),
            slippage_pct=execution.get("slippage_pct", 0.0),
            spread=execution.get("spread", 0.0),
            fill_pct=execution.get("fill_pct", 100.0),
            delay_impact_pct=copytrade.get("delay_impact_pct", 0.0) if copytrade else 0.0,
            outcome=settlement.get("outcome"),
            won=settlement.get("won"),
            settled_at=settlement.get("timestamp"),
            gross_pnl=settlement.get("gross_profit", 0.0),
            net_pnl=settlement.get("net_profit", 0.0),
            gross_payout=settlement.get("gross_payout", 0.0),
            settlement_status=settlement.get("status", "pending"),
            force_exit_reason=settlement.get("force_exit_reason"),
            streak_length=context.get("streak_length", 0),
            confidence=context.get("confidence", 0.0),
            arbitrage_edge=context.get("arbitrage_edge", 0.0),
            copied_from=copytrade.get("wallet") if copytrade else None,
            trader_name=copytrade.get("name") if copytrade else None,
            trader_direction=copytrade.get("direction") if copytrade else None,
            trader_amount=copytrade.get("amount") if copytrade else None,
            trader_price=copytrade.get("price") if copytrade else None,
            trader_timestamp=copytrade.get("timestamp") if copytrade else None,
            copy_delay_ms=copytrade.get("delay_ms") if copytrade else None,
            delay_model_breakdown=copytrade.get("delay_breakdown") if copytrade else None,
            bankroll_before=risk.get("bankroll_before", 0.0),
            bankroll_after=risk.get("bankroll_after", 0.0),
            consecutive_wins=risk.get("consecutive_wins", 0),
            consecutive_losses=risk.get("consecutive_losses", 0),
            market_volume=market.get("volume", 0.0),
            best_bid=execution.get("best_bid", 0.0),
            best_ask=execution.get("best_ask", 0.0),
            price_at_signal=execution.get("entry_price", 0.0),
            price_movement_pct=execution.get("price_movement_pct", 0.0),
            market_bias=context.get("market_bias", "neutral"),
            hour_utc=timing.get("hour_utc"),
            minute_of_hour=timing.get("minute"),
            day_of_week=timing.get("day_of_week"),
            seconds_into_window=timing.get("seconds_into_window"),
            window_close_time=market.get("window_close"),
            session_trade_number=session.get("trade_number"),
            session_wins_before=session.get("wins_before"),
            session_losses_before=session.get("losses_before"),
            session_pnl_before=session.get("pnl_before"),
            paper=context.get("mode", "paper") == "paper",
        )

    def summary(self) -> str:
        """One-line summary."""
        status = "✓ WON" if self.won else "✗ LOST" if self.won is False else "⏳ PENDING"
        return (
            f"{self.direction.upper()} ${self.amount:.2f} @ {self.execution_price:.3f} "
            f"| {status} | PnL: ${self.net_pnl:+.2f}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# TRADING STATE
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradingState:
    """Persistent bot state with risk management, per-strategy tracking,
    and comprehensive trade history.
    """

    trades: list[Trade] = field(default_factory=list)
    bankroll: float = field(default_factory=lambda: Config.INITIAL_BANKROLL)
    peak_bankroll: float = field(default_factory=lambda: Config.INITIAL_BANKROLL)
    daily_bets: int = 0
    daily_pnl: float = 0.0
    last_reset_date: str = ""
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    circuit_breaker_active: bool = False
    circuit_breaker_until: int = 0

    # Per-strategy tracking
    strategy_stats: dict[str, dict] = field(default_factory=lambda: {
        "arbitrage": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        "streak": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        "copytrade": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        "spread_farmer": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
        "panic_reversal": {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0},
    })

    # History tracking
    _saved_trade_ids: set = field(default_factory=set)

    def reset_daily_if_needed(self):
        """Reset daily counters at midnight UTC."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            self.daily_bets = 0
            self.daily_pnl = 0.0
            self.last_reset_date = today

    def can_trade(self) -> tuple[bool, str]:
        """Check if we can trade based on all risk limits.

        Returns: (can_trade, reason)
        """
        self.reset_daily_if_needed()

        # Circuit breaker
        if self.circuit_breaker_active:
            now = int(time.time())
            if now < self.circuit_breaker_until:
                mins_left = (self.circuit_breaker_until - now) // 60
                return False, f"Circuit breaker active ({mins_left}m remaining)"
            else:
                self.circuit_breaker_active = False
                self.consecutive_losses = 0

        # Daily limits
        if self.daily_bets >= Config.MAX_DAILY_BETS:
            return False, f"Max daily bets reached ({Config.MAX_DAILY_BETS})"

        if self.daily_pnl <= -Config.MAX_DAILY_LOSS:
            return False, f"Max daily loss reached (${Config.MAX_DAILY_LOSS})"

        # Bankroll check
        if self.bankroll < Config.MIN_BET:
            return False, f"Bankroll too low (${self.bankroll:.2f} < ${Config.MIN_BET:.2f})"

        # Drawdown check
        if self.peak_bankroll > 0:
            drawdown = (self.peak_bankroll - self.bankroll) / self.peak_bankroll
            threshold = Config.get_drawdown_threshold_for_risk()
            if drawdown > threshold:
                return False, f"Drawdown exceeded ({drawdown:.1%} > {threshold:.1%})"

        # Concurrent positions check
        pending_count = sum(1 for t in self.trades if t.outcome is None)
        if pending_count >= Config.MAX_CONCURRENT_POSITIONS:
            return False, f"Max concurrent positions ({Config.MAX_CONCURRENT_POSITIONS})"

        return True, "OK"

    def record_trade(self, trade: Trade):
        """Record a new trade and update counters."""
        self.trades.append(trade)
        self.daily_bets += 1

        if trade.strategy in self.strategy_stats:
            self.strategy_stats[trade.strategy]["trades"] += 1

    def settle_trade(
        self,
        trade_or_id: "Trade | str",
        outcome: str,
        market: Optional[Market] = None,
    ):
        """Settle a trade and calculate all P&L details.

        Args:
            trade_or_id: Trade object or trade ID string
            outcome: Market outcome ("up" or "down")
            market: Optional market for extra resolution data
        """
        if isinstance(trade_or_id, str):
            trade = next((t for t in self.trades if t.id == trade_or_id), None)
            if not trade:
                return
        else:
            trade = trade_or_id

        trade.outcome = outcome
        trade.won = (trade.direction == outcome)
        trade.settled_at = int(time.time() * 1000)
        trade.settlement_status = "settled"

        # Resolution timing
        resolution_time = int(time.time())
        trade.resolution_time = resolution_time
        if trade.window_close_time:
            trade.resolution_delay_seconds = resolution_time - trade.window_close_time

        # Use execution price (includes slippage)
        exec_price = trade.execution_price if trade.execution_price > 0 else trade.entry_price

        # Calculate shares
        trade.shares = trade.amount / exec_price if exec_price > 0 else 0

        if trade.won:
            # Win: receive $1 per share
            trade.gross_payout = trade.shares
            trade.gross_pnl = trade.gross_payout - trade.amount

            # Fee on profit only
            fee_pct = trade.fee_pct if trade.fee_pct > 0 else 0.0
            trade.fee_amount = trade.gross_pnl * fee_pct if trade.gross_pnl > 0 else 0.0

            trade.net_pnl = trade.gross_pnl - trade.fee_amount
        else:
            # Loss: lose entire amount
            trade.gross_payout = 0.0
            trade.gross_pnl = -trade.amount
            trade.fee_amount = 0.0
            trade.net_pnl = -trade.amount

        # Update bankroll
        self.bankroll += trade.net_pnl
        trade.bankroll_after = self.bankroll

        # Update peak
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll

        # Update daily PnL
        self.daily_pnl += trade.net_pnl

        # Update strategy stats
        if trade.strategy in self.strategy_stats:
            self.strategy_stats[trade.strategy]["pnl"] += trade.net_pnl
            if trade.won:
                self.strategy_stats[trade.strategy]["wins"] += 1
            else:
                self.strategy_stats[trade.strategy]["losses"] += 1

        # Update win/loss streaks
        if trade.won:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0

            # Circuit breaker check
            cb_threshold = Config.get_circuit_breaker_for_risk()
            if self.consecutive_losses >= cb_threshold:
                self.circuit_breaker_active = True
                self.circuit_breaker_until = int(time.time()) + (Config.COOLDOWN_MINUTES * 60)
                print(
                    f"[CIRCUIT BREAKER] {cb_threshold} consecutive losses. "
                    f"Pausing for {Config.COOLDOWN_MINUTES} minutes."
                )

    def record_settled_trade(self, trade: Trade):
        """Record a trade that's already settled (e.g., from spread farmer).
        
        Unlike record_trade + settle_trade, this handles trades that were
        placed and settled by an internal strategy engine.
        """
        self.trades.append(trade)
        self.daily_bets += 1

        # Ensure strategy stats exist
        if trade.strategy not in self.strategy_stats:
            self.strategy_stats[trade.strategy] = {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0}

        self.strategy_stats[trade.strategy]["trades"] += 1
        self.strategy_stats[trade.strategy]["pnl"] += trade.net_pnl
        if trade.won:
            self.strategy_stats[trade.strategy]["wins"] += 1
        else:
            self.strategy_stats[trade.strategy]["losses"] += 1

        # Update bankroll
        self.bankroll += trade.net_pnl
        trade.bankroll_after = self.bankroll

        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll

        self.daily_pnl += trade.net_pnl
        self.save()

    def mark_pending_as_force_exit(self, reason: str):
        """Mark all pending trades as force_exit before shutdown."""
        for trade in self.trades:
            if trade.settlement_status == "pending" and trade.outcome is None:
                trade.settlement_status = "force_exit"
                trade.force_exit_reason = reason

    def get_pending_trades(self) -> list[Trade]:
        """Get all unsettled trades."""
        return [t for t in self.trades if t.outcome is None and t.settlement_status == "pending"]

    def get_position_exposure(self) -> float:
        """Total USD exposure in pending trades."""
        return sum(t.amount for t in self.get_pending_trades())

    def get_strategy_pnl(self, strategy: str) -> float:
        """Get total P&L for a specific strategy."""
        stats = self.strategy_stats.get(strategy, {})
        return stats.get("pnl", 0.0)

    def get_statistics(self) -> dict:
        """Get comprehensive statistics."""
        settled = [t for t in self.trades if t.outcome is not None]
        pending = [t for t in self.trades if t.outcome is None]
        wins = [t for t in settled if t.won]
        losses = [t for t in settled if not t.won]

        total_pnl = sum(t.net_pnl for t in settled)

        return {
            "bankroll": self.bankroll,
            "peak_bankroll": self.peak_bankroll,
            "drawdown_pct": (self.peak_bankroll - self.bankroll) / self.peak_bankroll * 100
                if self.peak_bankroll > 0 else 0,
            "total_trades": len(self.trades),
            "settled_trades": len(settled),
            "pending_trades": len(pending),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(settled) * 100 if settled else 0,
            "total_pnl": total_pnl,
            "daily_pnl": self.daily_pnl,
            "daily_bets": self.daily_bets,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_active": self.circuit_breaker_active,
            "strategy_stats": self.strategy_stats,
            "avg_win": sum(t.net_pnl for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.net_pnl for t in losses) / len(losses) if losses else 0,
            "largest_win": max((t.net_pnl for t in wins), default=0),
            "largest_loss": min((t.net_pnl for t in losses), default=0),
            "total_fees": sum(t.fee_amount for t in settled),
            "avg_slippage_pct": sum(t.slippage_pct for t in settled) / len(settled) if settled else 0,
            "exposure": self.get_position_exposure(),
        }

    # ─── Persistence ──────────────────────────────────────────────────────

    def save(self):
        """Save state to disk with working state and full history."""
        # Working state (last 100 trades for fast loading)
        data = {
            "trades": [t.to_nested_json() for t in self.trades[-100:]],
            "bankroll": self.bankroll,
            "peak_bankroll": self.peak_bankroll,
            "daily_bets": self.daily_bets,
            "daily_pnl": self.daily_pnl,
            "last_reset_date": self.last_reset_date,
            "consecutive_losses": self.consecutive_losses,
            "consecutive_wins": self.consecutive_wins,
            "circuit_breaker_active": self.circuit_breaker_active,
            "circuit_breaker_until": self.circuit_breaker_until,
            "strategy_stats": self.strategy_stats,
        }

        with open(Config.TRADES_FILE, "w") as f:
            json.dump(data, f, indent=2)

        # Append to full history
        self._append_to_full_history()
        self._update_settled_in_history()

    def _append_to_full_history(self):
        """Append only new trades to the full history file."""
        history_file = Config.HISTORY_FILE

        new_trades = []
        for t in self.trades:
            if t.id not in self._saved_trade_ids:
                new_trades.append(t)
                self._saved_trade_ids.add(t.id)

        if not new_trades:
            return

        existing = []
        if os.path.exists(history_file):
            try:
                with open(history_file) as f:
                    existing = json.load(f)
            except (json.JSONDecodeError, Exception):
                existing = []

        for t in new_trades:
            existing.append(t.to_nested_json())

        with open(history_file, "w") as f:
            json.dump(existing, f, indent=2)

    def _update_settled_in_history(self):
        """Update settled trades in the full history file."""
        history_file = Config.HISTORY_FILE
        if not os.path.exists(history_file):
            return

        settled_trades = {
            t.id: t for t in self.trades
            if t.settlement_status in ("settled", "force_exit")
        }
        if not settled_trades:
            return

        try:
            with open(history_file) as f:
                history = json.load(f)
        except Exception:
            return

        updated = 0
        for i, entry in enumerate(history):
            trade_id = entry.get("id")
            if not trade_id:
                continue
            settlement = entry.get("settlement", {})
            if settlement.get("status", "pending") != "pending":
                continue
            if trade_id in settled_trades:
                t = settled_trades[trade_id]
                history[i]["settlement"] = {
                    "status": t.settlement_status,
                    "outcome": t.outcome,
                    "won": t.won,
                    "timestamp": t.settled_at,
                    "resolution_delay_sec": t.resolution_delay_seconds,
                    "gross_payout": t.gross_payout,
                    "gross_profit": t.gross_pnl,
                    "fee_amount": t.fee_amount,
                    "net_profit": t.net_pnl,
                }
                if t.settlement_status == "force_exit":
                    history[i]["settlement"]["force_exit_reason"] = t.force_exit_reason
                if t.shares > 0 and "position" in history[i]:
                    history[i]["position"]["shares"] = t.shares
                # Update risk section with final bankroll
                if "risk" in history[i]:
                    history[i]["risk"]["bankroll_after"] = t.bankroll_after
                updated += 1

        if updated > 0:
            with open(history_file, "w") as f:
                json.dump(history, f, indent=2)

    @classmethod
    def load(cls) -> 'TradingState':
        """Load state from disk."""
        state = cls()

        if os.path.exists(Config.TRADES_FILE):
            try:
                with open(Config.TRADES_FILE) as f:
                    data = json.load(f)

                trades_data = data.get("trades", [])
                loaded = []
                for t in trades_data:
                    if "id" in t or "market" in t:
                        loaded.append(Trade.from_nested_json(t))
                    elif "market_type" in t:
                        loaded.append(Trade.from_dict(t))
                state.trades = loaded

                state.bankroll = data.get("bankroll", Config.INITIAL_BANKROLL)
                state.peak_bankroll = data.get("peak_bankroll", Config.INITIAL_BANKROLL)
                state.daily_bets = data.get("daily_bets", 0)
                state.daily_pnl = data.get("daily_pnl", 0.0)
                state.last_reset_date = data.get("last_reset_date", "")
                state.consecutive_losses = data.get("consecutive_losses", 0)
                state.consecutive_wins = data.get("consecutive_wins", 0)
                state.circuit_breaker_active = data.get("circuit_breaker_active", False)
                state.circuit_breaker_until = data.get("circuit_breaker_until", 0)
                state.strategy_stats = data.get("strategy_stats", state.strategy_stats)

            except Exception as e:
                print(f"[trader] Error loading state: {e}")

        # Load saved trade IDs from history
        if os.path.exists(Config.HISTORY_FILE):
            try:
                with open(Config.HISTORY_FILE) as f:
                    history = json.load(f)
                for t in history:
                    tid = t.get("id", "")
                    if tid:
                        state._saved_trade_ids.add(tid)
            except Exception:
                pass

        return state


# ═══════════════════════════════════════════════════════════════════════════════
# PAPER TRADER
# ═══════════════════════════════════════════════════════════════════════════════

class PaperTrader:
    """Paper trading simulation with realistic fees, slippage, and fill modeling.

    Queries real orderbook data for accurate execution simulation.
    """

    def __init__(self, state: TradingState, market_cache: Optional[MarketDataCache] = None):
        self._client = PolymarketClient(timeout=Config.REST_TIMEOUT)
        self._market_cache = market_cache
        self.state = state

    def place_bet(
        self,
        market: Market,
        direction: str,
        amount: float,
        strategy: str,
        **kwargs,
    ) -> Optional[Trade]:
        """Place a simulated bet with realistic execution.

        Returns None if order is rejected.
        """
        # Validate
        can_trade, reason = self.state.can_trade()
        if not can_trade:
            print(f"[PAPER] ❌ Trade rejected: {reason}")
            return None

        if amount < Config.MIN_BET:
            print(f"[PAPER] ❌ Amount ${amount:.2f} below minimum ${Config.MIN_BET:.2f}")
            return None

        if amount > self.state.bankroll:
            amount = self.state.bankroll  # Cap at bankroll
            if amount < Config.MIN_BET:
                print(f"[PAPER] ❌ Insufficient bankroll (${self.state.bankroll:.2f})")
                return None

        token_id = market.get_token_id(direction)
        entry_price = market.get_price(direction)
        executed_at = int(time.time() * 1000)

        if not token_id:
            print(f"[PAPER] ❌ No token ID for {direction}")
            return None

        # Default values
        fee_rate_bps = getattr(market, 'taker_fee_bps', 1000)
        execution_price = entry_price if entry_price > 0 else 0.5
        spread = 0.0
        slippage_pct = 0.0
        fill_pct = 100.0
        delay_impact_pct = 0.0
        delay_breakdown = None
        best_bid = 0.0
        best_ask = 0.0
        copy_delay_ms = kwargs.get("copy_delay_ms", 0)

        # Use precomputed execution or query orderbook
        precomputed = kwargs.pop("precomputed_execution", None)

        if precomputed:
            execution_price = precomputed.get("execution_price", execution_price)
            spread = precomputed.get("spread", 0.0)
            slippage_pct = precomputed.get("slippage_pct", 0.0)
            fill_pct = precomputed.get("fill_pct", 100.0)
            delay_impact_pct = precomputed.get("delay_impact_pct", 0.0)
            delay_breakdown = precomputed.get("delay_breakdown")
            best_bid = precomputed.get("best_bid", 0.0)
            best_ask = precomputed.get("best_ask", 0.0)
        else:
            try:
                if self._market_cache:
                    exec_result = self._market_cache.get_execution_price(
                        token_id, "BUY", amount, copy_delay_ms
                    )
                else:
                    exec_result = self._client.get_execution_price(
                        token_id, "BUY", amount, copy_delay_ms
                    )
                execution_price, spread, slippage_pct, fill_pct, delay_impact_pct, delay_breakdown = exec_result

                # Get best bid/ask
                if self._market_cache:
                    book = self._market_cache.get_orderbook(token_id)
                else:
                    book = self._client.get_orderbook(token_id)
                if book:
                    bids = book.get("bids", [])
                    asks = book.get("asks", [])
                    if bids:
                        best_bid = max(float(b["price"]) for b in bids)
                    if asks:
                        best_ask = min(float(a["price"]) for a in asks)
            except Exception as e:
                print(f"[PAPER] Warning: Could not fetch market data: {e}")

        # Fee calculation
        fee_pct = self._client.calculate_fee(execution_price, fee_rate_bps)

        # Adjust for partial fill
        filled_amount = amount * (fill_pct / 100.0)
        if fill_pct < 100.0:
            print(f"[PAPER] ⚠️ Partial fill: {fill_pct:.1f}% of ${amount:.2f} = ${filled_amount:.2f}")

        # Shares
        shares = filled_amount / execution_price if execution_price > 0 else 0

        # Price movement
        price_movement_pct = 0.0
        if entry_price > 0:
            price_movement_pct = ((execution_price - entry_price) / entry_price) * 100

        # Timing analysis
        exec_dt = datetime.fromtimestamp(executed_at / 1000, tz=timezone.utc)
        window_start = market.timestamp
        seconds_into_window = int(executed_at / 1000) - window_start
        window_close_time = window_start + market.market_type.interval_seconds

        # Market context
        opposite_price = market.down_price if direction == "up" else market.up_price
        if market.up_price > 0.52:
            market_bias = "bullish"
        elif market.down_price > 0.52:
            market_bias = "bearish"
        else:
            market_bias = "neutral"

        # Generate trade ID
        trade_id = f"{market.timestamp}_{executed_at}_{direction}_{strategy}"

        trade = Trade(
            id=trade_id,
            timestamp=market.timestamp,
            market_type=market.market_type,
            market_slug=market.slug,
            direction=direction,
            strategy=strategy,
            amount=filled_amount,
            requested_amount=amount,
            entry_price=entry_price if entry_price > 0 else 0.5,
            execution_price=execution_price,
            shares=shares,
            executed_at=executed_at,
            fee_rate_bps=fee_rate_bps,
            fee_pct=fee_pct,
            slippage_pct=slippage_pct,
            spread=spread,
            fill_pct=fill_pct,
            delay_impact_pct=delay_impact_pct,
            delay_model_breakdown=delay_breakdown,
            bankroll_before=self.state.bankroll,
            paper=True,
            market_volume=market.volume,
            best_bid=best_bid,
            best_ask=best_ask,
            price_at_signal=entry_price,
            price_movement_pct=price_movement_pct,
            opposite_price=opposite_price,
            market_bias=market_bias,
            hour_utc=exec_dt.hour,
            minute_of_hour=exec_dt.minute,
            day_of_week=exec_dt.weekday(),
            seconds_into_window=seconds_into_window,
            window_close_time=window_close_time,
            consecutive_wins=self.state.consecutive_wins,
            consecutive_losses=self.state.consecutive_losses,
            # Pass through copytrade fields
            copied_from=kwargs.get("copied_from"),
            trader_name=kwargs.get("trader_name"),
            trader_direction=kwargs.get("trader_direction"),
            trader_amount=kwargs.get("trader_amount"),
            trader_price=kwargs.get("trader_price"),
            trader_timestamp=kwargs.get("trader_timestamp"),
            copy_delay_ms=kwargs.get("copy_delay_ms"),
            confidence=kwargs.get("confidence", 0.0),
            streak_length=kwargs.get("streak_length", 0),
            arbitrage_edge=kwargs.get("arbitrage_edge", 0.0),
            session_trade_number=kwargs.get("session_trade_number"),
            session_wins_before=kwargs.get("session_wins_before"),
            session_losses_before=kwargs.get("session_losses_before"),
            session_pnl_before=kwargs.get("session_pnl_before"),
        )

        # Record trade
        self.state.record_trade(trade)
        self.state.save()

        # Log
        emoji = {"arbitrage": "⚡", "streak": "📈", "copytrade": "📋"}.get(strategy, "🎯")
        spread_cents = spread * 100
        if strategy == "copytrade" and kwargs.get("trader_name"):
            trader_name = kwargs["trader_name"]
            delay_info = f" | Delay: +{delay_impact_pct:.2f}%" if delay_impact_pct > 0 else ""
            print(
                f"[PAPER] {emoji} Copied {trader_name}: ${filled_amount:.2f} {direction.upper()} "
                f"@ {execution_price:.3f} | Fee: {fee_pct:.2%} | Spread: {spread_cents:.0f}¢{delay_info}"
            )
        else:
            print(
                f"[PAPER] {emoji} {strategy.upper()}: ${filled_amount:.2f} {direction.upper()} "
                f"@ {execution_price:.3f} on {market.slug} | Fee: {fee_pct:.2%} | Spread: {spread_cents:.0f}¢"
            )

        return trade


# ═══════════════════════════════════════════════════════════════════════════════
# LIVE TRADER
# ═══════════════════════════════════════════════════════════════════════════════

class LiveTrader:
    """Live trading via Polymarket CLOB API.

    Supports:
    - EOA/MetaMask wallets (signature_type=0)
    - Magic/proxy wallets (signature_type=1)
    - FOK (Fill-Or-Kill) market orders for immediate execution
    - Order status tracking and confirmation
    """

    MIN_ORDER_SIZE = 1.0

    def __init__(self, state: TradingState, market_cache: Optional[MarketDataCache] = None):
        if not Config.PRIVATE_KEY:
            raise ValueError("PRIVATE_KEY not set in .env")
        if Config.SIGNATURE_TYPE == 1 and not Config.FUNDER_ADDRESS:
            raise ValueError("FUNDER_ADDRESS required for proxy wallet (SIGNATURE_TYPE=1)")

        self._market_cache = market_cache
        self.state = state
        self._init_client()

    def _init_client(self):
        """Initialize py-clob-client with wallet credentials."""
        try:
            from py_clob_client.client import ClobClient

            if Config.SIGNATURE_TYPE == 1:
                self.client = ClobClient(
                    host=Config.CLOB_API,
                    key=Config.PRIVATE_KEY,
                    chain_id=Config.CHAIN_ID,
                    signature_type=1,
                    funder=Config.FUNDER_ADDRESS,
                )
            else:
                self.client = ClobClient(
                    host=Config.CLOB_API,
                    key=Config.PRIVATE_KEY,
                    chain_id=Config.CHAIN_ID,
                )

            # Retry API cred derivation (proxy can be flaky on first request)
            creds = None
            for attempt in range(5):
                try:
                    creds = self.client.create_or_derive_api_creds()
                    break
                except Exception as e:
                    print(f"[trader] API creds attempt {attempt+1}/5 failed: {e}")
                    import time as _t
                    _t.sleep(2)
            if not creds:
                raise RuntimeError("Failed to derive API creds after 5 attempts")
            self.client.set_api_creds(creds)

            wallet_type = "proxy" if Config.SIGNATURE_TYPE == 1 else "EOA"
            print(f"[trader] Live trading client initialized ({wallet_type} wallet)")

        except ImportError:
            raise ImportError("py-clob-client not installed. Run: pip install py-clob-client")
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to init trading client: {e}")

    def _validate_order(self, market: Market, direction: str, amount: float) -> tuple[bool, str]:
        """Validate order parameters."""
        if amount < self.MIN_ORDER_SIZE:
            return False, f"Order size ${amount:.2f} below minimum ${self.MIN_ORDER_SIZE:.2f}"
        token_id = market.get_token_id(direction)
        if not token_id:
            return False, f"No token ID for {direction}"
        if not market.accepting_orders:
            return False, f"Market {market.slug} not accepting orders"
        if market.closed:
            return False, f"Market {market.slug} is closed"
        return True, ""

    def _get_order_status(self, order_id: str, max_attempts: int = 5, poll_interval: float = 0.5) -> dict:
        """Poll for order status until filled or timeout."""
        for attempt in range(max_attempts):
            try:
                order = self.client.get_order(order_id)
                status = order.get("status", "unknown")

                if status in ("FILLED", "MATCHED"):
                    return {
                        "status": "filled",
                        "filled_size": float(order.get("size_matched", order.get("size", 0))),
                        "avg_price": float(order.get("price", 0)),
                        "order": order,
                    }
                elif status in ("CANCELED", "CANCELLED", "EXPIRED"):
                    return {"status": "cancelled", "filled_size": 0, "avg_price": 0, "order": order}
                else:
                    time.sleep(poll_interval)
            except Exception as e:
                print(f"[trader] Error polling order {order_id}: {e}")
                time.sleep(poll_interval)

        return {"status": "unknown", "filled_size": 0, "avg_price": 0, "order": None}

    def place_bet(
        self,
        market: Market,
        direction: str,
        amount: float,
        strategy: str,
        **kwargs,
    ) -> Optional[Trade]:
        """Place a live bet using FOK market order."""
        is_valid, error_msg = self._validate_order(market, direction, amount)
        if not is_valid:
            print(f"[LIVE] Order rejected: {error_msg}")
            return None

        kwargs.pop("precomputed_execution", None)

        token_id = market.get_token_id(direction)
        entry_price = market.get_price(direction)
        if entry_price <= 0:
            entry_price = 0.5

        executed_at = int(time.time() * 1000)
        order_id = None
        execution_price = entry_price
        filled_amount = amount

        fee_rate_bps = getattr(market, 'taker_fee_bps', 1000)
        fee_pct = PolymarketClient.calculate_fee(entry_price, fee_rate_bps)

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY
            from typing import cast

            fok_order_type = cast(OrderType, OrderType.FOK)

            market_order = MarketOrderArgs(
                token_id=token_id,
                amount=amount,
                side=BUY,
                order_type=fok_order_type,
            )

            signed_order = self.client.create_market_order(market_order)
            response = self.client.post_order(signed_order, fok_order_type)

            order_id = response.get("orderID", response.get("id", "unknown"))

            emoji = {"arbitrage": "⚡", "streak": "📈", "copytrade": "📋"}.get(strategy, "🎯")
            print(f"[LIVE] {emoji} {strategy.upper()}: ${amount:.2f} {direction.upper()} "
                  f"@ {entry_price:.2f} | order={order_id} (FOK)")

            # Poll for fill
            if order_id and not order_id.startswith("FAILED"):
                status_result = self._get_order_status(order_id)
                if status_result["status"] == "filled":
                    filled_amount = status_result["filled_size"] * status_result["avg_price"]
                    execution_price = status_result["avg_price"]
                    print(f"[LIVE] Filled: {status_result['filled_size']:.2f} shares @ {execution_price:.3f}")
                elif status_result["status"] == "cancelled":
                    print("[LIVE] Order cancelled (FOK not filled)")
                    return None

        except Exception as e:
            print(f"[LIVE] Order failed: {e}")
            order_id = f"FAILED:{e}"
            return None

        trade_id = f"{market.timestamp}_{executed_at}_{direction}_{strategy}"

        trade = Trade(
            id=trade_id,
            timestamp=market.timestamp,
            market_type=market.market_type,
            market_slug=market.slug,
            direction=direction,
            strategy=strategy,
            amount=filled_amount,
            requested_amount=amount,
            entry_price=entry_price,
            execution_price=execution_price,
            shares=filled_amount / execution_price if execution_price > 0 else 0,
            executed_at=executed_at,
            fee_rate_bps=fee_rate_bps,
            fee_pct=fee_pct,
            bankroll_before=self.state.bankroll,
            paper=False,
            order_id=order_id,
            confidence=kwargs.get("confidence", 0.0),
            streak_length=kwargs.get("streak_length", 0),
            arbitrage_edge=kwargs.get("arbitrage_edge", 0.0),
            copied_from=kwargs.get("copied_from"),
            trader_name=kwargs.get("trader_name"),
            trader_direction=kwargs.get("trader_direction"),
            trader_amount=kwargs.get("trader_amount"),
            trader_price=kwargs.get("trader_price"),
            trader_timestamp=kwargs.get("trader_timestamp"),
            copy_delay_ms=kwargs.get("copy_delay_ms"),
        )

        self.state.record_trade(trade)
        self.state.save()

        return trade
