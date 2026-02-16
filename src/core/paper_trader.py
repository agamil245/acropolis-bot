"""Standalone Paper Trading Engine for AcropolisBot.

Independent paper trading system that:
- Runs completely separately from live trading
- Uses REAL Polymarket price feeds for live market data
- Simulates order execution with realistic slippage
- Tracks its own virtual bankroll, positions, and P&L
- Logs every trade to a JSON file for export
- Can be started/stopped independently from the GUI
"""

import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional

from src.config import Config, LOCAL_TZ, MarketType
from src.core.polymarket import PolymarketClient, Market, MarketDataCache


@dataclass
class PaperTrade:
    """A single paper trade record."""
    id: str
    timestamp: int          # market window timestamp
    executed_at: int        # unix ms
    market_slug: str
    market_type: str
    strategy: str
    direction: str          # "up" / "down"
    side: str               # "BUY"
    amount: float           # USD
    entry_price: float      # displayed price
    execution_price: float  # simulated fill price
    shares: float
    slippage_pct: float = 0.0
    spread: float = 0.0
    fee_pct: float = 0.0

    # settlement
    outcome: Optional[str] = None
    won: Optional[bool] = None
    settled_at: Optional[int] = None
    gross_pnl: float = 0.0
    net_pnl: float = 0.0

    # context
    confidence: float = 0.0
    bankroll_before: float = 0.0
    bankroll_after: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PaperState:
    """Persistent state for the independent paper trading engine."""
    bankroll: float = 1000.0
    initial_bankroll: float = 1000.0
    peak_bankroll: float = 1000.0
    trades: list[PaperTrade] = field(default_factory=list)
    running: bool = False

    # --- stats helpers ---

    @property
    def settled_trades(self) -> list[PaperTrade]:
        return [t for t in self.trades if t.outcome is not None]

    @property
    def pending_trades(self) -> list[PaperTrade]:
        return [t for t in self.trades if t.outcome is None]

    @property
    def wins(self) -> int:
        return sum(1 for t in self.settled_trades if t.won)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.settled_trades if not t.won)

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl for t in self.settled_trades)

    @property
    def win_rate(self) -> float:
        s = self.settled_trades
        return (self.wins / len(s) * 100) if s else 0.0

    def get_stats(self) -> dict:
        return {
            "bankroll": round(self.bankroll, 2),
            "initial_bankroll": round(self.initial_bankroll, 2),
            "peak_bankroll": round(self.peak_bankroll, 2),
            "total_pnl": round(self.total_pnl, 2),
            "win_rate": round(self.win_rate, 1),
            "wins": self.wins,
            "losses": self.losses,
            "total_trades": len(self.trades),
            "pending_trades": len(self.pending_trades),
            "settled_trades": len(self.settled_trades),
            "drawdown_pct": round(
                (self.peak_bankroll - self.bankroll) / self.peak_bankroll * 100
                if self.peak_bankroll > 0 else 0, 1
            ),
        }

    # --- persistence ---

    def save(self, path: str):
        data = {
            "bankroll": self.bankroll,
            "initial_bankroll": self.initial_bankroll,
            "peak_bankroll": self.peak_bankroll,
            "trades": [t.to_dict() for t in self.trades[-500:]],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    @classmethod
    def load(cls, path: str, initial_bankroll: float = 1000.0) -> "PaperState":
        state = cls(
            bankroll=initial_bankroll,
            initial_bankroll=initial_bankroll,
            peak_bankroll=initial_bankroll,
        )
        if os.path.exists(path):
            try:
                with open(path) as f:
                    data = json.load(f)
                state.bankroll = data.get("bankroll", initial_bankroll)
                state.initial_bankroll = data.get("initial_bankroll", initial_bankroll)
                state.peak_bankroll = data.get("peak_bankroll", initial_bankroll)
                for td in data.get("trades", []):
                    state.trades.append(PaperTrade(**td))
            except Exception as e:
                print(f"[paper] Error loading state: {e}")
        return state


class PaperTradingEngine:
    """Independent paper trading engine.

    Uses real Polymarket data but fake money. Completely independent
    from the main bot's live/paper mode.
    """

    def __init__(
        self,
        initial_bankroll: float = None,
        log_file: str = None,
        market_cache: Optional[MarketDataCache] = None,
    ):
        self._initial_bankroll = initial_bankroll or Config.PAPER_INITIAL_BANKROLL
        self._log_file = log_file or Config.PAPER_LOG_FILE
        self._client = PolymarketClient()
        self._market_cache = market_cache

        self.state = PaperState.load(self._log_file, self._initial_bankroll)

    # --- trade execution ---

    def place_trade(
        self,
        market: Market,
        direction: str,
        amount: float,
        strategy: str = "manual",
        confidence: float = 0.0,
    ) -> Optional[PaperTrade]:
        """Simulate placing a trade against real orderbook data."""

        if amount > self.state.bankroll:
            amount = self.state.bankroll
        if amount < 1.0:
            return None

        token_id = market.get_token_id(direction)
        entry_price = market.get_price(direction)
        if not token_id or entry_price <= 0:
            return None

        # Get real execution price from orderbook
        execution_price = entry_price
        spread = 0.0
        slippage_pct = 0.0

        try:
            if self._market_cache:
                result = self._market_cache.get_execution_price(token_id, "BUY", amount)
            else:
                result = self._client.get_execution_price(token_id, "BUY", amount)
            execution_price = result[0]
            spread = result[1]
            slippage_pct = result[2]
        except Exception:
            pass

        fee_rate_bps = getattr(market, "taker_fee_bps", 1000)
        fee_pct = self._client.calculate_fee(execution_price, fee_rate_bps)
        shares = amount / execution_price if execution_price > 0 else 0

        executed_at = int(time.time() * 1000)
        trade_id = f"paper_{market.timestamp}_{executed_at}_{direction}"

        trade = PaperTrade(
            id=trade_id,
            timestamp=market.timestamp,
            executed_at=executed_at,
            market_slug=market.slug,
            market_type=market.market_type.value,
            strategy=strategy,
            direction=direction,
            side="BUY",
            amount=round(amount, 2),
            entry_price=entry_price,
            execution_price=round(execution_price, 4),
            shares=round(shares, 4),
            slippage_pct=round(slippage_pct, 3),
            spread=round(spread, 4),
            fee_pct=round(fee_pct, 4),
            confidence=confidence,
            bankroll_before=round(self.state.bankroll, 2),
        )

        self.state.trades.append(trade)
        self.state.save(self._log_file)

        ts = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
        print(
            f"[{ts}] [PAPER-SIM] 📝 {strategy.upper()}: ${amount:.2f} {direction.upper()} "
            f"@ {execution_price:.3f} on {market.slug}"
        )
        return trade

    def settle_trade(self, trade: PaperTrade, outcome: str, market: Optional[Market] = None):
        """Settle a paper trade."""
        trade.outcome = outcome
        trade.won = trade.direction == outcome
        trade.settled_at = int(time.time() * 1000)

        exec_price = trade.execution_price or trade.entry_price
        shares = trade.amount / exec_price if exec_price > 0 else 0
        trade.shares = shares

        if trade.won:
            gross_payout = shares  # $1/share
            trade.gross_pnl = gross_payout - trade.amount
            fee_amount = trade.gross_pnl * trade.fee_pct if trade.gross_pnl > 0 else 0
            trade.net_pnl = trade.gross_pnl - fee_amount
        else:
            trade.gross_pnl = -trade.amount
            trade.net_pnl = -trade.amount

        self.state.bankroll += trade.net_pnl
        trade.bankroll_after = round(self.state.bankroll, 2)

        if self.state.bankroll > self.state.peak_bankroll:
            self.state.peak_bankroll = self.state.bankroll

        self.state.save(self._log_file)

    def check_settlements(self):
        """Check pending trades for resolution using real market data."""
        for trade in self.state.pending_trades:
            try:
                mt = MarketType(trade.market_type)
                market = self._client.get_market(mt, trade.timestamp, use_cache=False)
                if market and market.closed and market.outcome:
                    self.settle_trade(trade, market.outcome, market)
                    emoji = "✅" if trade.won else "❌"
                    ts = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
                    print(
                        f"[{ts}] [PAPER-SIM] {emoji} {trade.market_slug}: "
                        f"{trade.direction.upper()} → {market.outcome.upper()} | "
                        f"PnL: ${trade.net_pnl:+.2f} | Bank: ${self.state.bankroll:.2f}"
                    )
            except Exception as e:
                print(f"[PAPER-SIM] Settlement error for {trade.id}: {e}")

    def reset(self, initial_bankroll: float = None):
        """Reset paper trading state."""
        br = initial_bankroll or self._initial_bankroll
        self.state = PaperState(
            bankroll=br,
            initial_bankroll=br,
            peak_bankroll=br,
        )
        self.state.save(self._log_file)

    def get_trades_json(self) -> list[dict]:
        """Get all trades as dicts (for export)."""
        return [t.to_dict() for t in reversed(self.state.trades)]

    def get_recent_trades(self, count: int = 50) -> list[dict]:
        """Get recent trades formatted for GUI."""
        trades = list(reversed(self.state.trades))[:count]
        result = []
        for t in trades:
            result.append({
                "id": t.id,
                "timestamp": datetime.fromtimestamp(
                    t.executed_at / 1000, tz=LOCAL_TZ
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "market": t.market_slug,
                "strategy": t.strategy,
                "direction": t.direction.upper(),
                "amount": round(t.amount, 2),
                "price": round(t.execution_price, 4),
                "outcome": t.outcome.upper() if t.outcome else "PENDING",
                "won": t.won,
                "pnl": round(t.net_pnl, 2),
                "status": "settled" if t.outcome else "pending",
            })
        return result
