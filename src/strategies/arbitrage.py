"""
Micro-arbitrage strategy — THE PRIMARY MONEY MAKER.

Exploits structural pricing inefficiencies in crypto minute-markets on
Polymarket.  When the sum of YES + NO prices drops below a configurable
threshold (default $0.98) we lock in a near-risk-free edge by purchasing the
underpriced side.

Key features
────────────
• Multi-market scanning across BTC / ETH / SOL × 5 min / 15 min
• Per-market configurable thresholds and exposure limits
• Millisecond-resolution opportunity tracking
• Orderbook-aware execution sizing (walks the book)
• Dual-side arbitrage when both sides are sufficiently cheap
• Cooldown / dedup to avoid hammering the same market
• Full statistics: opportunities found vs executed, avg edge, hit-rate
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import Config, MarketType, MARKET_PROFILES
from src.core.polymarket import Market


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ArbitrageSignal:
    """Signal emitted when an arbitrage opportunity is detected."""

    market: Market
    direction: str                   # "up", "down", or "both"
    edge_pct: float                  # percentage edge (100 * (1 - combined))
    recommended_size: float          # suggested USD amount
    up_price: float
    down_price: float
    combined_price: float
    reason: str
    market_type: MarketType = MarketType.BTC_5M
    confidence: float = 1.0          # arb is ~risk-free
    timestamp_ms: int = 0
    book_depth_up: float = 0.0       # USD depth on best ask (YES)
    book_depth_down: float = 0.0     # USD depth on best ask (NO)
    spread_up: float = 0.0
    spread_down: float = 0.0

    def __post_init__(self):
        if self.timestamp_ms == 0:
            self.timestamp_ms = int(time.time() * 1000)


@dataclass
class MarketThreshold:
    """Per-market-type threshold configuration."""

    market_type: MarketType
    threshold: float = 0.98          # combined price ceiling
    min_edge_pct: float = 1.0        # minimum percentage edge
    max_exposure: float = 500.0      # max USD tied up in this market
    check_interval: float = 0.25     # seconds between scans
    cooldown: float = 2.0            # seconds between signals for same slug


@dataclass
class ArbitrageStats:
    """Cumulative statistics for the arbitrage strategy."""

    opportunities_found: int = 0
    opportunities_executed: int = 0
    opportunities_skipped: int = 0
    total_edge_pct_sum: float = 0.0  # for average calculation
    best_edge_pct: float = 0.0
    worst_edge_pct: float = 100.0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    total_exposure_time_ms: int = 0
    per_market: dict = field(default_factory=dict)   # market_type -> sub-stats

    @property
    def avg_edge_pct(self) -> float:
        if self.opportunities_found == 0:
            return 0.0
        return self.total_edge_pct_sum / self.opportunities_found

    @property
    def execution_rate(self) -> float:
        if self.opportunities_found == 0:
            return 0.0
        return self.opportunities_executed / self.opportunities_found

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        if total == 0:
            return 0.0
        return self.wins / total

    def record_opportunity(self, signal: ArbitrageSignal, executed: bool):
        self.opportunities_found += 1
        self.total_edge_pct_sum += signal.edge_pct
        self.best_edge_pct = max(self.best_edge_pct, signal.edge_pct)
        self.worst_edge_pct = min(self.worst_edge_pct, signal.edge_pct)
        if executed:
            self.opportunities_executed += 1
        else:
            self.opportunities_skipped += 1

        # Per-market tracking
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
            "win_rate": f"{self.win_rate:.1%}",
            "wins": self.wins,
            "losses": self.losses,
            "per_market": self.per_market,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def walk_book(levels: list[dict], amount_usd: float) -> tuple[float, float, float]:
    """Walk an orderbook side to estimate execution price and fill.

    Args:
        levels: list of {"price": str, "size": str} sorted appropriately
        amount_usd: USD we want to spend

    Returns:
        (avg_execution_price, filled_usd, total_shares)
    """
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
    """Return (best_price, depth_usd_at_best) from an orderbook snapshot."""
    if not book:
        return 0.0, 0.0
    levels = book.get(side, [])
    if not levels:
        return 0.0, 0.0
    best = levels[0]
    price = float(best["price"])
    size = float(best["size"])
    return price, price * size


# ═══════════════════════════════════════════════════════════════════════════════
# STRATEGY CLASS
# ═══════════════════════════════════════════════════════════════════════════════

# Default thresholds for each market type
_DEFAULT_THRESHOLDS: dict[MarketType, MarketThreshold] = {}

def _build_default_thresholds():
    """Build default per-market thresholds from MARKET_PROFILES / Config."""
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


class ArbitrageStrategy:
    """
    Production arbitrage scanner.

    Lifecycle:
        1. ``evaluate(market, bankroll)``  — single market check
        2. ``scan_all(markets, bankroll)`` — batch multi-market check
        3. ``on_trade_placed(amount)``     — update exposure
        4. ``on_trade_settled(amount, pnl, won)`` — release exposure, update stats
    """

    def __init__(
        self,
        thresholds: Optional[dict[MarketType, MarketThreshold]] = None,
        market_cache=None,
    ):
        self.thresholds = thresholds or dict(_DEFAULT_THRESHOLDS)
        self.market_cache = market_cache  # optional MarketDataCache for orderbooks

        # Exposure tracking — per market type
        self._exposure: dict[MarketType, float] = {mt: 0.0 for mt in MarketType}
        self._total_exposure: float = 0.0
        self._global_max_exposure: float = getattr(Config, "ARB_MAX_EXPOSURE", 500.0)

        # Signal dedup / cooldown  (slug -> last_signal_time)
        self._last_signal: dict[str, float] = {}

        # Statistics
        self.stats = ArbitrageStats()

        # Scanning state
        self._scan_count: int = 0
        self._last_scan_time: float = 0.0
        self._last_scan_duration_ms: float = 0.0

    # ── core evaluation ───────────────────────────────────────────────────

    def evaluate(
        self,
        market: Market,
        bankroll: float,
        market_type: MarketType = MarketType.BTC_5M,
        book_up: Optional[dict] = None,
        book_down: Optional[dict] = None,
    ) -> Optional[ArbitrageSignal]:
        """Evaluate a single market for arbitrage.

        Args:
            market: Market object with current prices
            bankroll: Available USD bankroll
            market_type: Which crypto/interval market this is
            book_up: Optional pre-fetched YES orderbook
            book_down: Optional pre-fetched NO orderbook

        Returns:
            ArbitrageSignal or None
        """
        if market.closed or not market.accepting_orders:
            return None

        # Prices
        up_price = market.up_price
        down_price = market.down_price
        combined = up_price + down_price

        # Per-market-type thresholds
        cfg = self.thresholds.get(market_type, _DEFAULT_THRESHOLDS.get(market_type))
        if cfg is None:
            cfg = MarketThreshold(market_type=market_type)

        edge_pct = (1.0 - combined) * 100.0

        # Check threshold
        if combined >= cfg.threshold or edge_pct < cfg.min_edge_pct:
            return None

        # Cooldown check
        now = time.time()
        slug = getattr(market, "slug", "") or ""
        last = self._last_signal.get(slug, 0.0)
        if now - last < cfg.cooldown:
            return None

        # Exposure check
        current_mt_exposure = self._exposure.get(market_type, 0.0)
        remaining_mt = cfg.max_exposure - current_mt_exposure
        remaining_global = self._global_max_exposure - self._total_exposure
        available_exposure = min(remaining_mt, remaining_global)

        if available_exposure < getattr(Config, "ARB_MIN_BET", 1.0):
            return None

        # ── Determine side ────────────────────────────────────────────────
        # Buy the cheaper side for higher payout.  If roughly equal, buy both
        # (split position) when edge is large enough.
        price_diff = abs(up_price - down_price)
        dual_side = edge_pct >= 3.0 and price_diff < 0.03

        if dual_side:
            direction = "both"
            buy_price = max(up_price, down_price)  # worst-case for sizing
        elif up_price < down_price:
            direction = "up"
            buy_price = up_price
        elif down_price < up_price:
            direction = "down"
            buy_price = down_price
        else:
            direction = "up"
            buy_price = up_price

        # ── Book-aware sizing ─────────────────────────────────────────────
        book_depth_up, spread_up = 0.0, 0.0
        book_depth_down, spread_down = 0.0, 0.0

        if book_up:
            _, book_depth_up = estimate_book_depth(book_up, "asks")
            best_bid_up = float(book_up.get("bids", [{}])[0].get("price", 0)) if book_up.get("bids") else 0
            best_ask_up = float(book_up.get("asks", [{}])[0].get("price", 0)) if book_up.get("asks") else 0
            spread_up = best_ask_up - best_bid_up

        if book_down:
            _, book_depth_down = estimate_book_depth(book_down, "asks")
            best_bid_dn = float(book_down.get("bids", [{}])[0].get("price", 0)) if book_down.get("bids") else 0
            best_ask_dn = float(book_down.get("asks", [{}])[0].get("price", 0)) if book_down.get("asks") else 0
            spread_down = best_ask_dn - best_bid_dn

        # Size: scale with edge, cap at exposure & bankroll limits
        edge_factor = min(edge_pct / 2.0, 1.0)
        base_size = min(
            available_exposure * edge_factor,
            getattr(Config, "MAX_BET", 100.0),
            bankroll * 0.10,
        )

        # If we have book depth info, don't exceed 80 % of available liquidity
        if direction == "up" and book_depth_up > 0:
            base_size = min(base_size, book_depth_up * 0.8)
        elif direction == "down" and book_depth_down > 0:
            base_size = min(base_size, book_depth_down * 0.8)
        elif direction == "both":
            min_depth = min(
                book_depth_up if book_depth_up > 0 else float("inf"),
                book_depth_down if book_depth_down > 0 else float("inf"),
            )
            if min_depth < float("inf"):
                base_size = min(base_size, min_depth * 0.8)

        recommended_size = max(getattr(Config, "ARB_MIN_BET", 1.0), base_size)

        # Build human-readable reason
        reason = (
            f"ARB {market_type.value} {slug}: "
            f"YES=${up_price:.4f} NO=${down_price:.4f} "
            f"combined=${combined:.4f} (<${cfg.threshold}) "
            f"edge={edge_pct:.2f}% → BUY {direction.upper()} ${recommended_size:.2f}"
        )

        signal = ArbitrageSignal(
            market=market,
            direction=direction,
            edge_pct=edge_pct,
            recommended_size=recommended_size,
            up_price=up_price,
            down_price=down_price,
            combined_price=combined,
            reason=reason,
            market_type=market_type,
            confidence=1.0,
            book_depth_up=book_depth_up,
            book_depth_down=book_depth_down,
            spread_up=spread_up,
            spread_down=spread_down,
        )

        # Cooldown update
        self._last_signal[slug] = now

        return signal

    # ── batch scanning ────────────────────────────────────────────────────

    def scan_all(
        self,
        markets_by_type: dict[MarketType, list[Market]],
        bankroll: float,
    ) -> list[ArbitrageSignal]:
        """Scan multiple market types for arbitrage opportunities.

        Args:
            markets_by_type: {MarketType: [Market, ...]}
            bankroll: current bankroll

        Returns:
            list of ArbitrageSignal sorted by edge (best first)
        """
        scan_start = time.time()
        self._scan_count += 1
        signals: list[ArbitrageSignal] = []

        for mt, markets in markets_by_type.items():
            if mt not in getattr(Config, "ACTIVE_MARKETS", list(MarketType)):
                continue

            for mkt in markets:
                # Optionally grab orderbooks from cache
                book_up, book_down = None, None
                if self.market_cache:
                    up_token = getattr(mkt, "up_token_id", None)
                    dn_token = getattr(mkt, "down_token_id", None)
                    if up_token:
                        try:
                            book_up = self.market_cache.get_orderbook(up_token)
                        except Exception:
                            pass
                    if dn_token:
                        try:
                            book_down = self.market_cache.get_orderbook(dn_token)
                        except Exception:
                            pass

                sig = self.evaluate(mkt, bankroll, mt, book_up, book_down)
                if sig:
                    signals.append(sig)

        # Sort by edge descending
        signals.sort(key=lambda s: s.edge_pct, reverse=True)

        self._last_scan_time = scan_start
        self._last_scan_duration_ms = (time.time() - scan_start) * 1000

        return signals

    def evaluate_all_markets(
        self,
        markets: list[Market],
        bankroll: float,
        market_type: MarketType = MarketType.BTC_5M,
    ) -> list[ArbitrageSignal]:
        """Convenience wrapper: evaluate a flat list of markets of one type."""
        return self.scan_all({market_type: markets}, bankroll)

    # ── exposure management ───────────────────────────────────────────────

    def update_exposure(self, amount: float, market_type: MarketType = MarketType.BTC_5M):
        """Called when a trade is placed."""
        self._exposure[market_type] = self._exposure.get(market_type, 0.0) + amount
        self._total_exposure += amount

    def release_exposure(self, amount: float, market_type: MarketType = MarketType.BTC_5M):
        """Called when a trade is settled."""
        self._exposure[market_type] = max(0.0, self._exposure.get(market_type, 0.0) - amount)
        self._total_exposure = max(0.0, self._total_exposure - amount)

    @property
    def current_exposure(self) -> float:
        return self._total_exposure

    # ── statistics ────────────────────────────────────────────────────────

    def record_opportunity(self, signal: ArbitrageSignal, executed: bool):
        self.stats.record_opportunity(signal, executed)

    def record_settlement(self, pnl: float, won: bool, exposure_time_ms: int = 0):
        self.stats.record_settlement(pnl, won, exposure_time_ms)

    def get_stats(self) -> dict:
        """Full strategy stats including per-market breakdown."""
        exposure_by_market = {
            mt.value: {"exposure": exp, "max": self.thresholds.get(mt, MarketThreshold(mt)).max_exposure}
            for mt, exp in self._exposure.items()
            if exp > 0
        }

        return {
            "total_exposure": self._total_exposure,
            "global_max_exposure": self._global_max_exposure,
            "utilization_pct": (
                (self._total_exposure / self._global_max_exposure * 100)
                if self._global_max_exposure > 0
                else 0.0
            ),
            "exposure_by_market": exposure_by_market,
            "scan_count": self._scan_count,
            "last_scan_duration_ms": round(self._last_scan_duration_ms, 1),
            **self.stats.to_dict(),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STANDALONE HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

def calculate_arbitrage_pnl(
    amount: float,
    entry_price: float,
    won: bool,
    fee_pct: float,
) -> tuple[float, float, float]:
    """Calculate P&L for a settled arbitrage position.

    Args:
        amount: USD spent
        entry_price: price per share at entry
        won: whether the outcome matched our side
        fee_pct: fee as a decimal (0.025 = 2.5 %)

    Returns:
        (gross_profit, fee_amount, net_profit)
    """
    shares = amount / entry_price if entry_price > 0 else 0.0

    if won:
        gross_payout = shares  # $1 per share
        gross_profit = gross_payout - amount
        fee_amount = gross_profit * fee_pct if gross_profit > 0 else 0.0
        net_profit = gross_profit - fee_amount
    else:
        gross_profit = -amount
        fee_amount = 0.0
        net_profit = -amount

    return gross_profit, fee_amount, net_profit


def detect_dual_side_opportunity(
    up_price: float,
    down_price: float,
    threshold: float = 0.98,
) -> Optional[dict]:
    """Quick check whether buying both YES and NO is profitable.

    If YES + NO < threshold we can buy 1 share of each for < $1 and
    guarantee $1 back regardless of outcome.

    Returns dict with edge info or None.
    """
    combined = up_price + down_price
    if combined >= threshold:
        return None

    cost_per_pair = combined
    profit_per_pair = 1.0 - cost_per_pair
    edge_pct = profit_per_pair * 100.0

    return {
        "combined": combined,
        "cost_per_pair": cost_per_pair,
        "profit_per_pair": profit_per_pair,
        "edge_pct": edge_pct,
        "up_price": up_price,
        "down_price": down_price,
    }
