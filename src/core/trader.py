"""Trading execution and state management."""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

from src.config import Config, LOCAL_TZ, MarketType
from src.core.polymarket import Market


@dataclass
class Trade:
    """Record of a trade with full details."""
    
    # Core fields
    id: str
    timestamp: int
    market_type: MarketType
    market_slug: str
    direction: str  # "up" or "down"
    strategy: str  # "arbitrage", "streak", "copytrade"
    
    # Execution
    amount: float
    entry_price: float
    execution_price: float
    shares: float
    executed_at: int  # unix ms
    
    # Fees & slippage
    fee_pct: float
    fee_amount: float
    slippage_pct: float
    spread: float
    
    # Settlement
    outcome: Optional[str] = None
    won: Optional[bool] = None
    settled_at: Optional[int] = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    
    # Strategy specific
    streak_length: int = 0
    confidence: float = 0.0
    arbitrage_edge: float = 0.0
    copied_from: Optional[str] = None
    
    # Risk metrics
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0
    
    # Mode
    paper: bool = True

    def to_dict(self) -> dict:
        """Convert to dict for JSON serialization."""
        d = asdict(self)
        d['market_type'] = self.market_type.value
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'Trade':
        """Create Trade from dict."""
        data = data.copy()
        if 'market_type' in data:
            data['market_type'] = MarketType(data['market_type'])
        return cls(**data)


@dataclass
class TradingState:
    """Bot state with risk management."""
    
    trades: list[Trade] = field(default_factory=list)
    bankroll: float = 100.0
    peak_bankroll: float = 100.0
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
    })

    def reset_daily_if_needed(self):
        """Reset daily counters if new day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            self.daily_bets = 0
            self.daily_pnl = 0.0
            self.last_reset_date = today

    def can_trade(self) -> tuple[bool, str]:
        """Check if we can trade based on risk limits."""
        self.reset_daily_if_needed()
        
        # Check circuit breaker
        if self.circuit_breaker_active:
            now = int(time.time())
            if now < self.circuit_breaker_until:
                mins_left = (self.circuit_breaker_until - now) // 60
                return False, f"Circuit breaker active ({mins_left}m remaining)"
            else:
                self.circuit_breaker_active = False
                self.consecutive_losses = 0

        # Check daily limits
        if self.daily_bets >= Config.MAX_DAILY_BETS:
            return False, f"Max daily bets reached ({Config.MAX_DAILY_BETS})"
        
        if self.daily_pnl <= -Config.MAX_DAILY_LOSS:
            return False, f"Max daily loss reached (${Config.MAX_DAILY_LOSS})"

        # Check bankroll
        if self.bankroll < Config.MIN_BET:
            return False, f"Bankroll too low (${self.bankroll:.2f})"

        # Check drawdown
        drawdown = (self.peak_bankroll - self.bankroll) / self.peak_bankroll if self.peak_bankroll > 0 else 0
        if drawdown > Config.DRAWDOWN_THRESHOLD:
            return False, f"Drawdown exceeded ({drawdown:.1%} > {Config.DRAWDOWN_THRESHOLD:.1%})"

        return True, "OK"

    def record_trade(self, trade: Trade):
        """Record a new trade."""
        self.trades.append(trade)
        self.daily_bets += 1
        
        # Update strategy stats
        if trade.strategy in self.strategy_stats:
            self.strategy_stats[trade.strategy]["trades"] += 1

    def settle_trade(self, trade_id: str, outcome: str, market: Optional[Market] = None):
        """Settle a trade and update state."""
        trade = next((t for t in self.trades if t.id == trade_id), None)
        if not trade:
            return

        trade.outcome = outcome
        trade.won = (trade.direction == outcome)
        trade.settled_at = int(time.time() * 1000)

        # Calculate P&L
        if trade.won:
            gross_payout = trade.shares  # $1 per share
            gross_profit = gross_payout - trade.amount
            fee_amount = gross_profit * trade.fee_pct if gross_profit > 0 else 0
            net_profit = gross_profit - fee_amount
        else:
            gross_profit = -trade.amount
            fee_amount = 0
            net_profit = -trade.amount

        trade.gross_pnl = gross_profit
        trade.net_pnl = net_profit
        trade.fee_amount = fee_amount

        # Update bankroll
        self.bankroll += net_profit
        trade.bankroll_after = self.bankroll
        
        # Update peak
        if self.bankroll > self.peak_bankroll:
            self.peak_bankroll = self.bankroll

        # Update daily PnL
        self.daily_pnl += net_profit

        # Update strategy stats
        if trade.strategy in self.strategy_stats:
            self.strategy_stats[trade.strategy]["pnl"] += net_profit
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

            # Check circuit breaker
            if self.consecutive_losses >= Config.CIRCUIT_BREAKER_LOSSES:
                self.circuit_breaker_active = True
                self.circuit_breaker_until = int(time.time()) + (Config.COOLDOWN_MINUTES * 60)
                print(f"[CIRCUIT BREAKER] {Config.CIRCUIT_BREAKER_LOSSES} consecutive losses. "
                      f"Pausing for {Config.COOLDOWN_MINUTES} minutes.")

    def get_statistics(self) -> dict:
        """Get comprehensive statistics."""
        settled = [t for t in self.trades if t.outcome is not None]
        pending = [t for t in self.trades if t.outcome is None]
        wins = [t for t in settled if t.won]
        losses = [t for t in settled if not t.won]

        return {
            "bankroll": self.bankroll,
            "peak_bankroll": self.peak_bankroll,
            "total_trades": len(self.trades),
            "settled_trades": len(settled),
            "pending_trades": len(pending),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(settled) * 100 if settled else 0,
            "total_pnl": self.bankroll - Config.INITIAL_BANKROLL,
            "daily_pnl": self.daily_pnl,
            "daily_bets": self.daily_bets,
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "circuit_breaker_active": self.circuit_breaker_active,
            "strategy_stats": self.strategy_stats,
            "avg_win": sum(t.net_pnl for t in wins) / len(wins) if wins else 0,
            "avg_loss": sum(t.net_pnl for t in losses) / len(losses) if losses else 0,
        }

    def save(self):
        """Save state to disk."""
        data = {
            "trades": [t.to_dict() for t in self.trades[-100:]],  # Keep last 100
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

    @classmethod
    def load(cls) -> 'TradingState':
        """Load state from disk."""
        state = cls(bankroll=Config.INITIAL_BANKROLL, peak_bankroll=Config.INITIAL_BANKROLL)
        
        if os.path.exists(Config.TRADES_FILE):
            try:
                with open(Config.TRADES_FILE) as f:
                    data = json.load(f)
                
                state.trades = [Trade.from_dict(t) for t in data.get("trades", [])]
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

        return state


class PaperTrader:
    """Paper trading simulation."""

    def __init__(self, state: TradingState):
        from src.core.polymarket import PolymarketClient
        self.client = PolymarketClient()
        self.state = state

    def place_bet(
        self,
        market: Market,
        direction: str,
        amount: float,
        strategy: str,
        **kwargs
    ) -> Optional[Trade]:
        """Place a simulated bet."""
        
        # Check if we can trade
        can_trade, reason = self.state.can_trade()
        if not can_trade:
            print(f"[PAPER] ❌ Trade rejected: {reason}")
            return None

        # Validate amount
        if amount < Config.MIN_BET:
            print(f"[PAPER] ❌ Amount ${amount:.2f} below minimum ${Config.MIN_BET:.2f}")
            return None

        if amount > self.state.bankroll:
            print(f"[PAPER] ❌ Insufficient bankroll (${self.state.bankroll:.2f})")
            return None

        # Get token and execution details
        token_id = market.up_token_id if direction == "up" else market.down_token_id
        entry_price = market.up_price if direction == "up" else market.down_price

        if not token_id:
            print(f"[PAPER] ❌ No token ID for {direction}")
            return None

        # Get execution price with slippage
        exec_price, spread, slippage_pct, fill_pct = self.client.get_execution_price(
            token_id, "BUY", amount
        )

        if fill_pct < 100:
            print(f"[PAPER] ⚠️ Partial fill: {fill_pct:.1f}%")
            amount = amount * (fill_pct / 100)

        # Calculate shares and fees
        shares = amount / exec_price if exec_price > 0 else 0
        fee_pct = self.client.calculate_fee(exec_price, market.taker_fee_bps)

        # Create trade
        trade_id = f"{market.timestamp}_{int(time.time() * 1000)}_{direction}_{strategy}"
        
        trade = Trade(
            id=trade_id,
            timestamp=market.timestamp,
            market_type=market.market_type,
            market_slug=market.slug,
            direction=direction,
            strategy=strategy,
            amount=amount,
            entry_price=entry_price,
            execution_price=exec_price,
            shares=shares,
            executed_at=int(time.time() * 1000),
            fee_pct=fee_pct,
            fee_amount=0,  # Calculated on settlement
            slippage_pct=slippage_pct,
            spread=spread,
            bankroll_before=self.state.bankroll,
            paper=True,
            **kwargs
        )

        # Record trade
        self.state.record_trade(trade)
        self.state.save()

        # Log
        emoji = "⚡" if strategy == "arbitrage" else "📈" if strategy == "streak" else "📋"
        print(f"[PAPER] {emoji} {strategy.upper()}: {direction.upper()} ${amount:.2f} "
              f"@ {exec_price:.4f} on {market.slug}")

        return trade
