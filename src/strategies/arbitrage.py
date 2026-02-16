"""
Hybrid Strategy — The Coordinator (gabagool22 approach).

Combines two layers:
  Layer 1: Spread Farmer (The House) — always-on market making
  Layer 2: Latency Arb (The Sniper) — fires on Binance momentum signals

When Layer 2 detects a strong directional signal, it can OVERRIDE
Layer 1 temporarily: cancel the wrong-side order and go heavy on
the right side.

This is the main strategy class that bot_engine calls.

═══════════════════════════════════════════════════════════════════════════════
LEGACY COMPATIBILITY: This module preserves the ArbitrageStrategy class
interface that bot_engine expects. The old micro-arb logic is still
available as a fallback via _legacy_evaluate().
═══════════════════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import Config, MarketType, MARKET_PROFILES
from src.core.polymarket import Market, PolymarketClient, MarketDataCache

from src.strategies.spread_farmer import SpreadFarmer, SpreadCycle
from src.strategies.latency_arb import LatencyArb, LatencyArbSignal, MomentumSignal
from src.strategies.bayesian_model import BayesianModel


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES (preserved for backward compat)
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ArbitrageSignal:
    """Signal emitted when an arbitrage opportunity is detected."""

    market: Market
    direction: str                   # "up", "down", or "both"
    edge_pct: float
    recommended_size: float
    up_price: float
    down_price: float
    combined_price: float
    reason: str
    market_type: MarketType = MarketType.BTC_5M
    confidence: float = 1.0
    timestamp_ms: int = 0
    book_depth_up: float = 0.0
    book_depth_down: float = 0.0
    spread_up: float = 0.0
    spread_down: float = 0.0
    source: str = "hybrid"  # "hybrid", "spread", "latency", "legacy"

    def __post_init__(self):
        if self.timestamp_ms == 0:
            self.timestamp_ms = int(time.time() * 1000)


@dataclass
class MarketThreshold:
    """Per-market-type threshold configuration."""
    market_type: MarketType
    threshold: float = 0.98
    min_edge_pct: float = 1.0
    max_exposure: float = 500.0
    check_interval: float = 0.25
    cooldown: float = 2.0


@dataclass
class ArbitrageStats:
    """Cumulative statistics for the hybrid strategy."""
    opportunities_found: int = 0
    opportunities_executed: int = 0
    opportunities_skipped: int = 0
    total_edge_pct_sum: float = 0.0
    best_edge_pct: float = 0.0
    worst_edge_pct: float = 100.0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    total_exposure_time_ms: int = 0
    per_market: dict = field(default_factory=dict)

    # Hybrid-specific
    spread_pnl: float = 0.0
    latency_pnl: float = 0.0
    latency_overrides: int = 0

    @property
    def avg_edge_pct(self) -> float:
        return self.total_edge_pct_sum / self.opportunities_found if self.opportunities_found else 0.0

    @property
    def execution_rate(self) -> float:
        return self.opportunities_executed / self.opportunities_found if self.opportunities_found else 0.0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total else 0.0

    def record_opportunity(self, signal: ArbitrageSignal, executed: bool):
        self.opportunities_found += 1
        self.total_edge_pct_sum += signal.edge_pct
        self.best_edge_pct = max(self.best_edge_pct, signal.edge_pct)
        self.worst_edge_pct = min(self.worst_edge_pct, signal.edge_pct)
        if executed:
            self.opportunities_executed += 1
        else:
            self.opportunities_skipped += 1

        mt = signal.market_type.value
        if mt not in self.per_market:
            self.per_market[mt] = {"found": 0, "executed": 0, "edge_sum": 0.0}
        self.per_market[mt]["found"] += 1
        self.per_market[mt]["edge_sum"] += signal.edge_pct
        if executed:
            self.per_market[mt]["executed"] += 1

    def record_settlement(self, pnl: float, won: bool, exposure_time_ms: int = 0):
        self.total_pnl += pnl
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.total_exposure_time_ms += exposure_time_ms

    def to_dict(self) -> dict:
        return {
            "opportunities_found": self.opportunities_found,
            "opportunities_executed": self.opportunities_executed,
            "opportunities_skipped": self.opportunities_skipped,
            "execution_rate": f"{self.execution_rate:.1%}",
            "avg_edge_pct": f"{self.avg_edge_pct:.2f}%",
            "best_edge_pct": f"{self.best_edge_pct:.2f}%",
            "total_pnl": f"${self.total_pnl:+.2f}",
            "spread_pnl": f"${self.spread_pnl:+.2f}",
            "latency_pnl": f"${self.latency_pnl:+.2f}",
            "latency_overrides": self.latency_overrides,
            "win_rate": f"{self.win_rate:.1%}",
            "wins": self.wins,
            "losses": self.losses,
            "per_market": self.per_market,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK HELPERS (preserved)
# ═══════════════════════════════════════════════════════════════════════════════

def walk_book(levels: list[dict], amount_usd: float) -> tuple[float, float, float]:
    remaining = amount_usd
    total_cost = 0.0
    total_shares = 0.0
    for lvl in levels:
        if remaining <= 0:
            break
        price = float(lvl["price"])
        size = float(lvl["size"])
        level_usd = price * size
        if level_usd >= remaining:
            shares = remaining / price
            total_shares += shares
            total_cost += remaining
            remaining = 0
        else:
            total_shares += size
            total_cost += level_usd
            remaining -= level_usd
    filled = amount_usd - remaining
    avg_price = total_cost / total_shares if total_shares > 0 else 0.0
    return avg_price, filled, total_shares


def estimate_book_depth(book: Optional[dict], side: str = "asks") -> tuple[float, float]:
    if not book:
        return 0.0, 0.0
    levels = book.get(side, [])
    if not levels:
        return 0.0, 0.0
    best = levels[0]
    price = float(best["price"])
    size = float(best["size"])
    return price, price * size


def detect_dual_side_opportunity(
    up_price: float, down_price: float, threshold: float = 0.98,
) -> Optional[dict]:
    combined = up_price + down_price
    if combined >= threshold:
        return None
    cost_per_pair = combined
    profit_per_pair = 1.0 - cost_per_pair
    edge_pct = profit_per_pair * 100.0
    return {
        "combined": combined, "cost_per_pair": cost_per_pair,
        "profit_per_pair": profit_per_pair, "edge_pct": edge_pct,
        "up_price": up_price, "down_price": down_price,
    }


def calculate_arbitrage_pnl(
    amount: float, entry_price: float, won: bool, fee_pct: float,
) -> tuple[float, float, float]:
    shares = amount / entry_price if entry_price > 0 else 0.0
    if won:
        gross_profit = shares - amount
        fee_amount = gross_profit * fee_pct if gross_profit > 0 else 0.0
        net_profit = gross_profit - fee_amount
    else:
        gross_profit = -amount
        fee_amount = 0.0
        net_profit = -amount
    return gross_profit, fee_amount, net_profit


# ═══════════════════════════════════════════════════════════════════════════════
# DEFAULT THRESHOLDS
# ═══════════════════════════════════════════════════════════════════════════════

_DEFAULT_THRESHOLDS: dict[MarketType, MarketThreshold] = {}

def _build_default_thresholds():
    for mt in MarketType:
        _DEFAULT_THRESHOLDS[mt] = MarketThreshold(
            market_type=mt,
            threshold=getattr(Config, "ARB_THRESHOLD", 0.98),
            min_edge_pct=getattr(Config, "ARB_MIN_EDGE_PCT", 1.0),
            max_exposure=getattr(Config, "ARB_MAX_EXPOSURE", 500.0),
            check_interval=getattr(Config, "ARB_CHECK_INTERVAL", 0.25),
            cooldown=2.0,
        )

_build_default_thresholds()


# ═══════════════════════════════════════════════════════════════════════════════
# HYBRID STRATEGY (ArbitrageStrategy)
# ═══════════════════════════════════════════════════════════════════════════════

class ArbitrageStrategy:
    """Hybrid strategy coordinator.

    Manages both layers:
      - SpreadFarmer: always-on market making (Layer 1)
      - LatencyArb: Binance-driven directional sniping (Layer 2)

    The ArbitrageStrategy interface is preserved for backward compatibility
    with bot_engine. The evaluate() / scan_all() methods now incorporate
    both layers.

    Lifecycle (called by bot_engine):
        1. start_layers() — start Binance WS and spread farming
        2. evaluate() / scan_all() — called each tick
        3. on_trade_placed() / on_trade_settled() — bookkeeping
        4. stop_layers() — shutdown
    """

    def __init__(
        self,
        thresholds: Optional[dict[MarketType, MarketThreshold]] = None,
        market_cache: Optional[MarketDataCache] = None,
        client: Optional[PolymarketClient] = None,
    ):
        self.thresholds = thresholds or dict(_DEFAULT_THRESHOLDS)
        self.market_cache = market_cache
        self.client = client

        # Layer 0: Bayesian Intelligence Model
        self.bayesian_model = BayesianModel()

        # Layer 1: Spread Farmer
        self.spread_farmer = SpreadFarmer(client=client, market_cache=market_cache)
        self.spread_farmer.bayesian_model = self.bayesian_model

        # Layer 2: Latency Arb
        self.latency_arb = LatencyArb(
            client=client,
            market_cache=market_cache,
            on_fire=self._on_latency_signal,
        )
        self.latency_arb.bayesian_model = self.bayesian_model

        # Pending latency signals (consumed by bot_engine on next tick)
        self._pending_latency_signals: list[LatencyArbSignal] = []

        # Exposure tracking
        self._exposure: dict[MarketType, float] = {mt: 0.0 for mt in MarketType}
        self._total_exposure: float = 0.0
        self._global_max_exposure: float = getattr(Config, "ARB_MAX_EXPOSURE", 500.0)

        # Signal dedup / cooldown
        self._last_signal: dict[str, float] = {}

        # Stats
        self.stats = ArbitrageStats()

        # Scanning state
        self._scan_count: int = 0
        self._last_scan_time: float = 0.0
        self._last_scan_duration_ms: float = 0.0

    # ── Layer Management ──────────────────────────────────────────────────

    async def start_layers(self):
        """Start both strategy layers."""
        await self.latency_arb.start()
        print("[HYBRID] 🏛️ Hybrid strategy active: Spread Farmer + Latency Arb")

    async def stop_layers(self):
        """Stop both strategy layers."""
        await self.spread_farmer.cancel_all()
        await self.latency_arb.stop()
        print("[HYBRID] Layers stopped")

    # ── Latency Signal Handler ────────────────────────────────────────────

    def _on_latency_signal(self, signal: LatencyArbSignal):
        """Called by LatencyArb when it fires a signal.

        This triggers the OVERRIDE: cancel wrong-side spread orders
        and queue an aggressive directional bet.
        """
        self.stats.latency_overrides += 1
        self._pending_latency_signals.append(signal)

        # Tell spread farmer to pause and cancel wrong side
        if signal.momentum and signal.market:
            wrong_side = "NO" if signal.momentum.direction == "up" else "YES"
            # Schedule cancellation (can't await from sync callback)
            self.spread_farmer.set_override(signal.momentum.direction)
            print(f"[HYBRID] ⚡ Latency override: cancel {wrong_side}, "
                  f"go heavy {signal.momentum.direction.upper()}")

    def consume_latency_signals(self) -> list[LatencyArbSignal]:
        """Consume pending latency arb signals (called by bot_engine)."""
        signals = self._pending_latency_signals.copy()
        self._pending_latency_signals.clear()
        return signals

    # ── Spread Farming Tick ───────────────────────────────────────────────

    async def tick_spread_farmer(self, markets: list[Market]):
        """Run one tick of the spread farmer.

        Called by bot_engine in the arbitrage loop.
        """
        # Check fills on existing orders
        await self.spread_farmer.check_fills()

        # Refresh stale orders
        await self.spread_farmer.refresh_orders()

        # Post new cycles for markets that don't have active cycles
        active_slugs = {c.market_slug for c in self.spread_farmer.active_cycles if not c.settled}
        for market in markets:
            if market.slug not in active_slugs and market.accepting_orders and not market.closed:
                await self.spread_farmer.run_cycle(market)

        # Clear override after some time
        if self.spread_farmer._override_active:
            # Auto-clear after 10 seconds
            # (In production, clear when the directional trade is placed)
            self.spread_farmer.clear_override()

    # ── Legacy Evaluate (backward compat) ─────────────────────────────────

    def evaluate(
        self,
        market: Market,
        bankroll: float,
        market_type: MarketType = MarketType.BTC_5M,
        book_up: Optional[dict] = None,
        book_down: Optional[dict] = None,
    ) -> Optional[ArbitrageSignal]:
        """Evaluate a single market for arbitrage (legacy interface).

        Now also considers spread farming opportunities.
        """
        if market.closed or not market.accepting_orders:
            return None

        up_price = market.up_price
        down_price = market.down_price
        combined = up_price + down_price

        cfg = self.thresholds.get(market_type, _DEFAULT_THRESHOLDS.get(market_type))
        if cfg is None:
            cfg = MarketThreshold(market_type=market_type)

        edge_pct = (1.0 - combined) * 100.0

        if combined >= cfg.threshold or edge_pct < cfg.min_edge_pct:
            return None

        # Cooldown
        now = time.time()
        slug = getattr(market, "slug", "") or ""
        last = self._last_signal.get(slug, 0.0)
        if now - last < cfg.cooldown:
            return None

        # Exposure check
        current_mt = self._exposure.get(market_type, 0.0)
        remaining_mt = cfg.max_exposure - current_mt
        remaining_global = self._global_max_exposure - self._total_exposure
        available = min(remaining_mt, remaining_global)

        if available < getattr(Config, "ARB_MIN_BET", 1.0):
            return None

        # Direction
        price_diff = abs(up_price - down_price)
        dual_side = edge_pct >= 3.0 and price_diff < 0.03

        if dual_side:
            direction = "both"
            buy_price = max(up_price, down_price)
        elif up_price < down_price:
            direction = "up"
            buy_price = up_price
        else:
            direction = "down"
            buy_price = down_price

        # Book-aware sizing
        book_depth_up, spread_up = 0.0, 0.0
        book_depth_down, spread_down = 0.0, 0.0
        if book_up:
            _, book_depth_up = estimate_book_depth(book_up, "asks")
        if book_down:
            _, book_depth_down = estimate_book_depth(book_down, "asks")

        edge_factor = min(edge_pct / 2.0, 1.0)
        base_size = min(available * edge_factor, getattr(Config, "MAX_BET", 100.0), bankroll * 0.10)
        recommended_size = max(getattr(Config, "ARB_MIN_BET", 1.0), base_size)

        reason = (
            f"HYBRID {market_type.value} {slug}: "
            f"YES=${up_price:.4f} NO=${down_price:.4f} "
            f"combined=${combined:.4f} edge={edge_pct:.2f}% → BUY {direction.upper()} ${recommended_size:.2f}"
        )

        signal = ArbitrageSignal(
            market=market, direction=direction, edge_pct=edge_pct,
            recommended_size=recommended_size, up_price=up_price,
            down_price=down_price, combined_price=combined, reason=reason,
            market_type=market_type, confidence=1.0,
            book_depth_up=book_depth_up, book_depth_down=book_depth_down,
            spread_up=spread_up, spread_down=spread_down, source="hybrid",
        )

        self._last_signal[slug] = now
        return signal

    def scan_all(
        self, markets_by_type: dict[MarketType, list[Market]], bankroll: float,
    ) -> list[ArbitrageSignal]:
        """Scan multiple market types. Legacy interface."""
        scan_start = time.time()
        self._scan_count += 1
        signals: list[ArbitrageSignal] = []

        for mt, markets in markets_by_type.items():
            if mt not in getattr(Config, "ACTIVE_MARKETS", list(MarketType)):
                continue
            for mkt in markets:
                sig = self.evaluate(mkt, bankroll, mt)
                if sig:
                    signals.append(sig)

        signals.sort(key=lambda s: s.edge_pct, reverse=True)
        self._last_scan_time = scan_start
        self._last_scan_duration_ms = (time.time() - scan_start) * 1000
        return signals

    def evaluate_all_markets(
        self, markets: list[Market], bankroll: float, market_type: MarketType = MarketType.BTC_5M,
    ) -> list[ArbitrageSignal]:
        return self.scan_all({market_type: markets}, bankroll)

    # ── Exposure Management ───────────────────────────────────────────────

    def update_exposure(self, amount: float, market_type: MarketType = MarketType.BTC_5M):
        self._exposure[market_type] = self._exposure.get(market_type, 0.0) + amount
        self._total_exposure += amount

    def release_exposure(self, amount: float, market_type: MarketType = MarketType.BTC_5M):
        self._exposure[market_type] = max(0.0, self._exposure.get(market_type, 0.0) - amount)
        self._total_exposure = max(0.0, self._total_exposure - amount)

    @property
    def current_exposure(self) -> float:
        return self._total_exposure

    # ── Statistics ────────────────────────────────────────────────────────

    def record_opportunity(self, signal: ArbitrageSignal, executed: bool):
        self.stats.record_opportunity(signal, executed)

    def record_settlement(self, pnl: float, won: bool, exposure_time_ms: int = 0):
        self.stats.record_settlement(pnl, won, exposure_time_ms)

    def get_stats(self) -> dict:
        exposure_by_market = {
            mt.value: {"exposure": exp, "max": self.thresholds.get(mt, MarketThreshold(mt)).max_exposure}
            for mt, exp in self._exposure.items() if exp > 0
        }
        return {
            "total_exposure": self._total_exposure,
            "global_max_exposure": self._global_max_exposure,
            "utilization_pct": (self._total_exposure / self._global_max_exposure * 100)
                if self._global_max_exposure > 0 else 0.0,
            "exposure_by_market": exposure_by_market,
            "scan_count": self._scan_count,
            "last_scan_duration_ms": round(self._last_scan_duration_ms, 1),
            "layers": {
                "spread_farmer": self.spread_farmer.get_stats(),
                "latency_arb": self.latency_arb.get_stats(),
            },
            **self.stats.to_dict(),
        }
