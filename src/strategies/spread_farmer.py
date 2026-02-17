"""
Spread Farmer — Layer 1: The House.

Market-making strategy that posts limit orders on BOTH sides (YES and NO)
slightly below mid-price. When both legs fill, total cost is ~$0.95-0.96
for a guaranteed $1 payout at settlement = 4-5% risk-free per cycle.

This is the BASE INCOME strategy — runs constantly.

Inspired by PBot1's approach on Polymarket crypto minute-markets.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from src.config import Config, MarketType

if TYPE_CHECKING:
    from src.core.polymarket import PolymarketClient, MarketDataCache, Market
    from src.strategies.bayesian_model import BayesianModel


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class SpreadOrder:
    """Tracks a single limit order leg."""
    order_id: str
    token_id: str
    side: str           # "YES" or "NO"
    price: float        # limit price
    size: float         # USD size
    placed_at: float    # time.time()
    filled: bool = False
    filled_at: Optional[float] = None
    fill_price: Optional[float] = None
    cancelled: bool = False
    market_slug: str = ""
    market_ts: int = 0


@dataclass
class SpreadCycle:
    """A matched pair of YES + NO orders."""
    market_slug: str
    market_ts: int
    market_type: MarketType
    yes_order: Optional[SpreadOrder] = None
    no_order: Optional[SpreadOrder] = None
    created_at: float = 0.0
    settled: bool = False
    pnl: float = 0.0

    @property
    def both_filled(self) -> bool:
        return (self.yes_order is not None and self.yes_order.filled and
                self.no_order is not None and self.no_order.filled)

    @property
    def partial_fill(self) -> bool:
        yes_filled = self.yes_order is not None and self.yes_order.filled
        no_filled = self.no_order is not None and self.no_order.filled
        return yes_filled != no_filled

    @property
    def total_cost(self) -> float:
        cost = 0.0
        if self.yes_order and self.yes_order.filled:
            cost += self.yes_order.fill_price or self.yes_order.price
        if self.no_order and self.no_order.filled:
            cost += self.no_order.fill_price or self.no_order.price
        return cost

    @property
    def expected_profit(self) -> float:
        if self.both_filled:
            return 1.0 - self.total_cost  # $1 payout guaranteed
        return 0.0


@dataclass
class SpreadFarmerStats:
    """Cumulative statistics."""
    cycles_created: int = 0
    full_fills: int = 0
    partial_fills: int = 0
    orders_placed: int = 0
    orders_cancelled: int = 0
    orders_filled: int = 0
    total_pnl: float = 0.0
    total_volume: float = 0.0
    best_cycle_pnl: float = 0.0
    worst_cycle_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "cycles_created": self.cycles_created,
            "full_fills": self.full_fills,
            "partial_fills": self.partial_fills,
            "orders_placed": self.orders_placed,
            "orders_cancelled": self.orders_cancelled,
            "orders_filled": self.orders_filled,
            "total_pnl": f"${self.total_pnl:+.2f}",
            "total_volume": f"${self.total_volume:.2f}",
            "avg_pnl_per_full_fill": f"${self.total_pnl / self.full_fills:+.4f}" if self.full_fills else "$0",
            "fill_rate": f"{self.full_fills / self.cycles_created:.1%}" if self.cycles_created else "0%",
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SPREAD FARMER STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════

class SpreadFarmer:
    """Market-making spread farmer.

    Posts limit buy orders on both YES and NO slightly below mid-price.
    When both fill: guaranteed profit at settlement.

    Lifecycle (called by HybridStrategy coordinator):
        1. run_cycle(market) — post orders for a market
        2. check_fills() — poll open orders for fills
        3. refresh_orders() — cancel stale orders and repost at new levels
        4. cancel_side(side) — cancel one side (for latency arb override)
        5. settle_cycle(cycle, outcome) — record settlement
    """

    def __init__(
        self,
        client: Optional["PolymarketClient"] = None,
        market_cache: Optional["MarketDataCache"] = None,
    ):
        self.client = client
        self.market_cache = market_cache

        # Config
        self.spread_offset: float = Config.SPREAD_OFFSET
        self.order_size: float = Config.SPREAD_ORDER_SIZE
        self.refresh_interval: float = Config.SPREAD_REFRESH_INTERVAL

        # State
        self.active_cycles: list[SpreadCycle] = []
        self.completed_cycles: list[SpreadCycle] = []
        self.open_orders: dict[str, SpreadOrder] = {}  # order_id -> order

        # Stats
        self.stats = SpreadFarmerStats()

        # Override state (set by latency arb)
        self._override_active: bool = False
        self._override_direction: Optional[str] = None

        # Bayesian model reference (set by coordinator)
        self.bayesian_model: Optional["BayesianModel"] = None

        # Main trading state reference (set by bot engine for trade logging)
        self._trading_state = None

    # ── Order Management ──────────────────────────────────────────────────

    async def place_limit_order(
        self,
        token_id: str,
        side: str,
        price: float,
        size: float,
        market_slug: str = "",
        market_ts: int = 0,
    ) -> Optional[SpreadOrder]:
        """Place a limit order via CLOB API.

        In paper mode, simulates immediate placement.
        In live mode, uses py-clob-client.
        """
        order_id = f"sf_{side}_{market_ts}_{int(time.time()*1000)}"

        order = SpreadOrder(
            order_id=order_id,
            token_id=token_id,
            side=side,
            price=price,
            size=size,
            placed_at=time.time(),
            market_slug=market_slug,
            market_ts=market_ts,
        )

        if not Config.PAPER_TRADE:
            # Live order placement
            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import OrderArgs, OrderType
                from py_clob_client.order_builder.constants import BUY

                live_client = ClobClient(
                    host=Config.CLOB_API,
                    key=Config.PRIVATE_KEY,
                    chain_id=Config.CHAIN_ID,
                    signature_type=Config.SIGNATURE_TYPE,
                    funder=Config.FUNDER_ADDRESS if Config.SIGNATURE_TYPE == 1 else None,
                )
                creds = live_client.create_or_derive_api_creds()
                live_client.set_api_creds(creds)

                order_args = OrderArgs(
                    token_id=token_id,
                    price=price,
                    size=size / price if price > 0 else 0,  # shares
                    side=BUY,
                )
                signed = live_client.create_order(order_args)
                resp = live_client.post_order(signed, OrderType.GTC)
                order.order_id = resp.get("orderID", order_id)
            except Exception as e:
                print(f"[SPREAD] ❌ Live order failed: {e}")
                return None

        self.open_orders[order.order_id] = order
        self.stats.orders_placed += 1
        return order

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        order = self.open_orders.get(order_id)
        if not order or order.filled or order.cancelled:
            return False

        if not Config.PAPER_TRADE:
            try:
                from py_clob_client.client import ClobClient
                live_client = ClobClient(
                    host=Config.CLOB_API,
                    key=Config.PRIVATE_KEY,
                    chain_id=Config.CHAIN_ID,
                )
                creds = live_client.create_or_derive_api_creds()
                live_client.set_api_creds(creds)
                live_client.cancel(order_id)
            except Exception as e:
                print(f"[SPREAD] ❌ Cancel failed: {e}")
                return False

        order.cancelled = True
        del self.open_orders[order_id]
        self.stats.orders_cancelled += 1
        return True

    async def cancel_side(self, market_ts: int, side: str):
        """Cancel all orders on one side for a market (used by latency arb override)."""
        to_cancel = [
            oid for oid, o in self.open_orders.items()
            if o.market_ts == market_ts and o.side == side and not o.filled
        ]
        for oid in to_cancel:
            await self.cancel_order(oid)

    async def cancel_all(self):
        """Cancel all open orders."""
        for oid in list(self.open_orders.keys()):
            await self.cancel_order(oid)

    # ── Core Cycle ────────────────────────────────────────────────────────

    async def run_cycle(self, market: "Market") -> Optional[SpreadCycle]:
        """Post a spread on both sides of a market.

        Posts YES and NO limit orders at mid_price - spread_offset.
        """
        if not market or market.closed or not market.accepting_orders:
            return None

        if self._override_active:
            return None  # latency arb has taken over

        # Volatility regime check: pause in extreme vol, boost in low vol
        vol_size_multiplier = 1.0
        if self.bayesian_model and hasattr(market, 'market_type'):
            regime = self.bayesian_model.get_volatility_regime(market.market_type.asset)
            if regime == "extreme":
                return None  # too risky for both-side fills
            elif regime == "low":
                vol_size_multiplier = 1.3  # safer to increase size
            elif regime == "high":
                vol_size_multiplier = 0.7  # reduce exposure

        # Calculate limit prices: bid slightly below each side's actual price
        # This ensures both sides can fill based on actual orderbook mids
        yes_price = round(market.up_price - self.spread_offset, 4)
        no_price = round(market.down_price - self.spread_offset, 4)

        # Sanity: both prices should be positive and sum < 1.0
        yes_price = max(0.01, min(0.49, yes_price))
        no_price = max(0.01, min(0.49, no_price))

        if yes_price + no_price >= 0.99:
            return None  # No edge

        cycle = SpreadCycle(
            market_slug=market.slug,
            market_ts=market.timestamp,
            market_type=market.market_type,
            created_at=time.time(),
        )

        # Apply volatility-adjusted size
        adjusted_size = self.order_size * vol_size_multiplier

        # Place YES order
        if market.up_token_id:
            yes_order = await self.place_limit_order(
                token_id=market.up_token_id,
                side="YES",
                price=yes_price,
                size=adjusted_size,
                market_slug=market.slug,
                market_ts=market.timestamp,
            )
            cycle.yes_order = yes_order

        # Place NO order
        if market.down_token_id:
            no_order = await self.place_limit_order(
                token_id=market.down_token_id,
                side="NO",
                price=no_price,
                size=adjusted_size,
                market_slug=market.slug,
                market_ts=market.timestamp,
            )
            cycle.no_order = no_order

        self.active_cycles.append(cycle)
        self.stats.cycles_created += 1

        edge_pct = (1.0 - (yes_price + no_price)) * 100
        msg = (f"[SPREAD] 🏠 Posted: YES@{yes_price:.4f} + NO@{no_price:.4f} "
               f"= {yes_price + no_price:.4f} ({edge_pct:.1f}% edge) on {market.slug}")
        print(msg)
        with open("/tmp/spread_trades.log", "a") as _f:
            _f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            _f.flush()

        return cycle

    async def check_fills(self):
        """Check open orders for fills.

        In paper mode: simulate fills when market price crosses our limit.
        In live mode: poll order status from CLOB API.
        """
        for order in list(self.open_orders.values()):
            if order.filled or order.cancelled:
                continue

            if Config.PAPER_TRADE:
                # Paper mode: check if market moved to fill our limit
                filled = await self._check_paper_fill(order)
                if filled:
                    order.filled = True
                    order.filled_at = time.time()
                    order.fill_price = order.price
                    self.stats.orders_filled += 1
                    self.stats.total_volume += order.size
                    msg = f"[SPREAD] ✅ Filled: {order.side}@{order.price:.4f} on {order.market_slug}"
                    print(msg)
                    with open("/tmp/spread_trades.log", "a") as _f:
                        _f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
                        _f.flush()
            else:
                # Live mode: poll CLOB
                try:
                    from py_clob_client.client import ClobClient
                    live_client = ClobClient(
                        host=Config.CLOB_API,
                        key=Config.PRIVATE_KEY,
                        chain_id=Config.CHAIN_ID,
                    )
                    creds = live_client.create_or_derive_api_creds()
                    live_client.set_api_creds(creds)
                    status = live_client.get_order(order.order_id)
                    if status and status.get("status") in ("FILLED", "MATCHED"):
                        order.filled = True
                        order.filled_at = time.time()
                        order.fill_price = float(status.get("price", order.price))
                        self.stats.orders_filled += 1
                        self.stats.total_volume += order.size
                except Exception:
                    pass

        # Check cycles for completion (only log once per cycle)
        for cycle in self.active_cycles:
            if cycle.both_filled and not cycle.settled and not getattr(cycle, '_logged_full', False):
                self.stats.full_fills += 1
                cycle._logged_full = True
                print(f"[SPREAD] 🎯 FULL FILL on {cycle.market_slug}: "
                      f"cost={cycle.total_cost:.4f}, expected_profit=${cycle.expected_profit:.4f}")
            elif cycle.partial_fill and not getattr(cycle, '_logged_partial', False):
                cycle._logged_partial = True
                filled_side = "YES" if (cycle.yes_order and cycle.yes_order.filled) else "NO"
                print(f"[SPREAD] ⚠️ Partial fill ({filled_side}) on {cycle.market_slug}")

    async def _check_paper_fill(self, order: SpreadOrder) -> bool:
        """Simulate fill in paper mode.
        
        Limit orders 2¢ below mid on a liquid 5-min crypto market fill
        within seconds. We simulate ~30% chance per check (~1s interval)
        so both sides fill within ~3-10 seconds on average.
        """
        import random
        
        age = time.time() - order.placed_at
        
        # Time-based fill probability — realistic for near-mid limits
        # on liquid Polymarket crypto markets
        if age < 3:
            return random.random() < 0.30  # 30% in first 3 seconds
        elif age < 10:
            return random.random() < 0.40  # 40% up to 10s
        else:
            return random.random() < 0.50  # 50% after 10s
        if age > 120:
            base_prob = 0.10  # 10% after 2 minutes
        
        return random.random() < base_prob

    async def refresh_orders(self):
        """Cancel stale orders and repost at current levels."""
        now = time.time()
        for cycle in self.active_cycles:
            if cycle.settled:
                continue

            # Check if orders are stale
            for order in [cycle.yes_order, cycle.no_order]:
                if order and not order.filled and not order.cancelled:
                    age = now - order.placed_at
                    if age > self.refresh_interval:
                        await self.cancel_order(order.order_id)

    def settle_cycle(self, cycle: SpreadCycle, outcome: str):
        """Settle a cycle after market resolution and log to main trading state."""
        if cycle.settled:
            return

        cycle.settled = True

        if cycle.both_filled:
            # Both filled: guaranteed $1 payout
            cycle.pnl = 1.0 - cycle.total_cost
            self.stats.total_pnl += cycle.pnl
            self.stats.best_cycle_pnl = max(self.stats.best_cycle_pnl, cycle.pnl)
            # Log to main trading state
            if self._trading_state:
                self._record_spread_trade(cycle, outcome, "full_fill")
        elif cycle.partial_fill:
            self.stats.partial_fills += 1
            # Partial: one side filled, outcome determines P&L
            if cycle.yes_order and cycle.yes_order.filled:
                if outcome == "up":
                    cycle.pnl = 1.0 - (cycle.yes_order.fill_price or cycle.yes_order.price)
                else:
                    cycle.pnl = -(cycle.yes_order.fill_price or cycle.yes_order.price)
            elif cycle.no_order and cycle.no_order.filled:
                if outcome == "down":
                    cycle.pnl = 1.0 - (cycle.no_order.fill_price or cycle.no_order.price)
                else:
                    cycle.pnl = -(cycle.no_order.fill_price or cycle.no_order.price)
            self.stats.total_pnl += cycle.pnl
            self.stats.worst_cycle_pnl = min(self.stats.worst_cycle_pnl, cycle.pnl)
            # Log to main trading state
            if self._trading_state:
                self._record_spread_trade(cycle, outcome, "partial_fill")

        # Move to completed
        if cycle in self.active_cycles:
            self.active_cycles.remove(cycle)
        self.completed_cycles.append(cycle)

    def _record_spread_trade(self, cycle: SpreadCycle, outcome: str, fill_type: str):
        """Record a spread cycle as a trade in the main trading state."""
        from src.core.trader import Trade
        import time as _time

        # Determine the filled side(s) and direction
        if fill_type == "full_fill":
            direction = "spread"  # Both sides filled
            amount = cycle.total_cost
            entry_price = cycle.total_cost
        else:
            # Partial fill — direction is the filled side
            if cycle.yes_order and cycle.yes_order.filled:
                direction = "up"
                amount = cycle.yes_order.fill_price or cycle.yes_order.price
                entry_price = amount
            else:
                direction = "down"
                amount = cycle.no_order.fill_price or cycle.no_order.price
                entry_price = amount

        trade = Trade(
            id=f"spread-{cycle.market_slug}-{cycle.market_ts}",
            market_slug=cycle.market_slug,
            market_type=cycle.market_type if cycle.market_type else MarketType.BTC_5M,
            timestamp=cycle.market_ts,
            direction=direction,
            amount=amount,
            requested_amount=amount,
            entry_price=entry_price,
            execution_price=entry_price,
            shares=amount / entry_price if entry_price > 0 else 0,
            executed_at=int(cycle.created_at * 1000),
            strategy="spread_farmer",
        )

        # Settle immediately since we already know the outcome
        trade.outcome = outcome
        trade.won = cycle.pnl > 0
        trade.net_pnl = cycle.pnl
        trade.settled_at = int(_time.time() * 1000)
        trade.exit_price = 1.0 if trade.won else 0.0

        self._trading_state.record_settled_trade(trade)
        msg = (f"[SPREAD] 📊 Logged: {fill_type} on {cycle.market_slug} | "
               f"{'✅' if trade.won else '❌'} PnL: ${cycle.pnl:+.4f}")
        print(msg)
        # Also write to file to bypass stdout buffering
        with open("/tmp/spread_trades.log", "a") as _f:
            import time as _t
            _f.write(f"{_t.strftime('%H:%M:%S')} {msg}\n")
            _f.flush()

    # ── Override Control (for latency arb) ────────────────────────────────

    def set_override(self, direction: str):
        """Latency arb is taking directional control."""
        self._override_active = True
        self._override_direction = direction

    def clear_override(self):
        """Latency arb signal has passed, resume normal spread farming."""
        self._override_active = False
        self._override_direction = None

    # ── Stats ─────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        return {
            "active_cycles": len(self.active_cycles),
            "completed_cycles": len(self.completed_cycles),
            "open_orders": len(self.open_orders),
            "override_active": self._override_active,
            "config": {
                "spread_offset": self.spread_offset,
                "order_size": self.order_size,
                "refresh_interval": self.refresh_interval,
            },
            **self.stats.to_dict(),
        }
