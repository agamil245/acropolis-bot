"""
Copytrade strategy — ported from reference copybot.py + copybot_v2.py.

Monitors profitable wallets on Polymarket and mirrors their trades on
crypto minute-markets.  Combines the v1 REST-polling approach with the
v2 enhancements: WebSocket integration, selective filtering, trader
scoring, and position mirroring with configurable multipliers.

Features
────────
• REST polling with configurable interval (default 1.5 s)
• Optional WebSocket for sub-second signal detection
• Selective copy filter (delay cap, fill-price range, market freshness)
• Trader performance scoring & ranking (win-rate, PnL, ROI)
• Position mirroring with per-trader multipliers
• Automatic trader blacklist / whitelist
• Full statistics: copy latency, fill quality, per-trader P&L
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

import requests

from src.config import Config, MarketType, MARKET_PROFILES


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class CopySignal:
    """Signal emitted when a tracked wallet makes a trade."""

    wallet: str
    direction: str          # "up" or "down"
    side: str               # "BUY" or "SELL"
    market_ts: int          # market window timestamp
    trade_ts: int           # when the trader placed the trade (epoch seconds)
    price: float            # price the trader got
    usdc_amount: float      # trader's bet size in USD
    trader_name: str        # display name or pseudonym
    market_type: MarketType = MarketType.BTC_5M
    delay_ms: int = 0       # ms since trader's trade (set at detection time)
    market_slug: str = ""
    token_id: str = ""


@dataclass
class TraderProfile:
    """Performance profile for a tracked wallet."""

    wallet: str
    name: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_volume: float = 0.0
    avg_trade_size: float = 0.0
    avg_copy_delay_ms: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    # Per-market-type stats
    market_stats: dict = field(default_factory=dict)
    # Timestamps
    first_seen: int = 0
    last_seen: int = 0
    # Multiplier override (1.0 = match their size, 2.0 = 2x, etc.)
    copy_multiplier: float = 1.0
    # Blacklist flag
    blacklisted: bool = False
    blacklist_reason: str = ""

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return (self.wins / total) if total > 0 else 0.0

    @property
    def roi(self) -> float:
        return (self.total_pnl / self.total_volume) if self.total_volume > 0 else 0.0

    @property
    def score(self) -> float:
        """Composite score for ranking traders.

        Combines win-rate, ROI, and volume with a Bayesian prior so
        traders with few trades don't dominate the leaderboard.
        """
        total = self.wins + self.losses
        if total < 3:
            return 0.0  # not enough data

        # Bayesian adjusted win-rate (prior = 50%, strength = 10)
        adj_wr = (self.wins + 5) / (total + 10)

        # ROI component (capped)
        roi_factor = max(-0.5, min(0.5, self.roi))

        # Volume factor (log scale, more volume = more confidence)
        import math
        vol_factor = min(1.0, math.log10(max(1, self.total_volume)) / 4.0)

        return (adj_wr * 0.5 + (0.5 + roi_factor) * 0.3 + vol_factor * 0.2)

    def record_trade(self, amount: float, delay_ms: int = 0):
        """Record a new copy trade (before outcome)."""
        self.total_trades += 1
        self.total_volume += amount
        self.avg_trade_size = self.total_volume / self.total_trades
        # Running average delay
        if self.avg_copy_delay_ms == 0:
            self.avg_copy_delay_ms = float(delay_ms)
        else:
            self.avg_copy_delay_ms = self.avg_copy_delay_ms * 0.9 + delay_ms * 0.1
        self.last_seen = int(time.time())
        if self.first_seen == 0:
            self.first_seen = self.last_seen

    def record_outcome(self, won: bool, pnl: float, market_type: MarketType = MarketType.BTC_5M):
        """Record the outcome of a copy trade."""
        if won:
            self.wins += 1
        else:
            self.losses += 1
        self.total_pnl += pnl
        self.best_trade_pnl = max(self.best_trade_pnl, pnl)
        self.worst_trade_pnl = min(self.worst_trade_pnl, pnl)

        mt = market_type.value
        if mt not in self.market_stats:
            self.market_stats[mt] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if won:
            self.market_stats[mt]["wins"] += 1
        else:
            self.market_stats[mt]["losses"] += 1
        self.market_stats[mt]["pnl"] += pnl

    def to_dict(self) -> dict:
        total = self.wins + self.losses
        return {
            "wallet": self.wallet,
            "name": self.name,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": f"{self.win_rate:.1%}",
            "total_pnl": f"${self.total_pnl:+.2f}",
            "roi": f"{self.roi:.1%}",
            "score": round(self.score, 3),
            "avg_delay_ms": round(self.avg_copy_delay_ms),
            "copy_multiplier": self.copy_multiplier,
            "blacklisted": self.blacklisted,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# TRADER SCOREBOARD
# ═══════════════════════════════════════════════════════════════════════════════

class TraderScoreboard:
    """Maintains performance profiles for all tracked traders.

    Used by the bot engine to decide copy multipliers and whether to
    auto-blacklist consistently unprofitable traders.
    """

    def __init__(self, wallets: Optional[list[str]] = None):
        self._profiles: dict[str, TraderProfile] = {}
        if wallets:
            for w in wallets:
                self._profiles[w.lower()] = TraderProfile(wallet=w.lower())

        # Auto-blacklist config
        self.auto_blacklist_enabled: bool = True
        self.min_trades_for_blacklist: int = 10
        self.blacklist_win_rate_threshold: float = 0.35  # below 35% → blacklist
        self.blacklist_pnl_threshold: float = -50.0       # lost > $50 → blacklist

    def get_profile(self, wallet: str) -> TraderProfile:
        w = wallet.lower()
        if w not in self._profiles:
            self._profiles[w] = TraderProfile(wallet=w)
        return self._profiles[w]

    def record_trade(self, wallet: str, amount: float, delay_ms: int = 0):
        self.get_profile(wallet).record_trade(amount, delay_ms)

    def record_outcome(self, wallet: str, won: bool, pnl: float, market_type: MarketType = MarketType.BTC_5M):
        profile = self.get_profile(wallet)
        profile.record_outcome(won, pnl, market_type)

        # Auto-blacklist check
        if self.auto_blacklist_enabled and not profile.blacklisted:
            total = profile.wins + profile.losses
            if total >= self.min_trades_for_blacklist:
                if profile.win_rate < self.blacklist_win_rate_threshold:
                    profile.blacklisted = True
                    profile.blacklist_reason = f"Win rate {profile.win_rate:.1%} < {self.blacklist_win_rate_threshold:.1%}"
                elif profile.total_pnl < self.blacklist_pnl_threshold:
                    profile.blacklisted = True
                    profile.blacklist_reason = f"PnL ${profile.total_pnl:.2f} < ${self.blacklist_pnl_threshold:.2f}"

    def is_blacklisted(self, wallet: str) -> bool:
        return self.get_profile(wallet).blacklisted

    def get_copy_multiplier(self, wallet: str) -> float:
        """Get the effective copy multiplier for a trader.

        High-scoring traders get higher multipliers; low-scoring ones
        get reduced exposure.
        """
        profile = self.get_profile(wallet)
        if profile.blacklisted:
            return 0.0
        if profile.copy_multiplier != 1.0:
            return profile.copy_multiplier  # manual override

        # Auto-scale based on score (only after enough data)
        total = profile.wins + profile.losses
        if total < 5:
            return 1.0  # default until we have data

        score = profile.score
        if score >= 0.65:
            return 1.5
        elif score >= 0.55:
            return 1.0
        elif score >= 0.45:
            return 0.7
        else:
            return 0.5

    def get_rankings(self) -> list[TraderProfile]:
        """Get all traders sorted by score descending."""
        profiles = list(self._profiles.values())
        profiles.sort(key=lambda p: p.score, reverse=True)
        return profiles

    def get_active_traders(self) -> list[TraderProfile]:
        """Get non-blacklisted traders sorted by score."""
        return [p for p in self.get_rankings() if not p.blacklisted]

    def get_stats(self) -> dict:
        rankings = self.get_rankings()
        return {
            "total_traders": len(self._profiles),
            "active_traders": len([p for p in rankings if not p.blacklisted]),
            "blacklisted": len([p for p in rankings if p.blacklisted]),
            "rankings": [p.to_dict() for p in rankings],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# SELECTIVE COPY FILTER (from copybot_v2)
# ═══════════════════════════════════════════════════════════════════════════════

class SelectiveCopyFilter:
    """Filters copy signals based on quality criteria.

    Ported from reference copybot_v2.py's SelectiveFilter.
    """

    def __init__(self, config: Optional[dict] = None):
        cfg = config or {}
        self.max_delay_ms: int = cfg.get(
            "max_delay_ms",
            getattr(Config, "SELECTIVE_MAX_DELAY_MS", 30000),
        )
        self.min_fill_price: float = cfg.get(
            "min_fill_price",
            getattr(Config, "SELECTIVE_MIN_FILL_PRICE", 0.30),
        )
        self.max_fill_price: float = cfg.get(
            "max_fill_price",
            getattr(Config, "SELECTIVE_MAX_FILL_PRICE", 0.75),
        )
        self.max_spread: float = cfg.get("max_spread", 0.05)
        self.min_depth_usd: float = cfg.get("min_depth_usd", 5.0)
        self.min_trader_score: float = cfg.get("min_trader_score", 0.0)

        # Stats
        self.total_evaluated: int = 0
        self.total_passed: int = 0
        self.rejection_reasons: dict[str, int] = defaultdict(int)

    def should_copy(
        self,
        signal: CopySignal,
        execution_info: Optional[dict] = None,
        trader_profile: Optional[TraderProfile] = None,
    ) -> tuple[bool, str]:
        """Decide whether to copy a signal.

        Args:
            signal: The copy signal to evaluate
            execution_info: Optional dict with execution_price, spread, depth, etc.
            trader_profile: Optional trader profile for score-based filtering

        Returns:
            (should_copy: bool, reason: str)
        """
        self.total_evaluated += 1
        exec_info = execution_info or {}

        # 1. Delay check
        delay = signal.delay_ms or exec_info.get("copy_delay_ms", 0)
        if delay > self.max_delay_ms:
            reason = f"Delay {delay}ms > max {self.max_delay_ms}ms"
            self.rejection_reasons["delay"] += 1
            return False, reason

        # 2. Fill price check
        fill_price = exec_info.get("execution_price", signal.price)
        if fill_price < self.min_fill_price:
            reason = f"Fill price {fill_price:.3f} < min {self.min_fill_price}"
            self.rejection_reasons["price_too_low"] += 1
            return False, reason
        if fill_price > self.max_fill_price:
            reason = f"Fill price {fill_price:.3f} > max {self.max_fill_price}"
            self.rejection_reasons["price_too_high"] += 1
            return False, reason

        # 3. Spread check
        spread = exec_info.get("spread", 0.0)
        if spread > self.max_spread:
            reason = f"Spread {spread:.4f} > max {self.max_spread}"
            self.rejection_reasons["spread"] += 1
            return False, reason

        # 4. Depth check
        depth = exec_info.get("depth_at_best", 0.0)
        if depth > 0 and depth < self.min_depth_usd:
            reason = f"Depth ${depth:.2f} < min ${self.min_depth_usd:.2f}"
            self.rejection_reasons["depth"] += 1
            return False, reason

        # 5. Trader score check
        if trader_profile and self.min_trader_score > 0:
            if trader_profile.score < self.min_trader_score:
                reason = f"Trader score {trader_profile.score:.3f} < min {self.min_trader_score}"
                self.rejection_reasons["trader_score"] += 1
                return False, reason

        # 6. Blacklist check
        if trader_profile and trader_profile.blacklisted:
            reason = f"Trader blacklisted: {trader_profile.blacklist_reason}"
            self.rejection_reasons["blacklisted"] += 1
            return False, reason

        self.total_passed += 1
        return True, "all checks passed"

    @property
    def pass_rate(self) -> float:
        if self.total_evaluated == 0:
            return 0.0
        return self.total_passed / self.total_evaluated

    def get_stats(self) -> dict:
        return {
            "total_evaluated": self.total_evaluated,
            "total_passed": self.total_passed,
            "pass_rate": f"{self.pass_rate:.1%}",
            "rejection_reasons": dict(self.rejection_reasons),
            "config": {
                "max_delay_ms": self.max_delay_ms,
                "min_fill_price": self.min_fill_price,
                "max_fill_price": self.max_fill_price,
                "max_spread": self.max_spread,
                "min_depth_usd": self.min_depth_usd,
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# COPYTRADE MONITOR
# ═══════════════════════════════════════════════════════════════════════════════

class CopytradeMonitor:
    """Monitor wallets for trades across all market types.

    Combines v1 REST polling with v2 enhancements (fast polling, connection
    pooling, immediate-poll hooks for WebSocket triggers).
    """

    # Pattern: {asset}-updown-{interval}-{timestamp}
    MARKET_PATTERN = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")

    def __init__(
        self,
        wallets: Optional[list[str]] = None,
        poll_interval: Optional[float] = None,
        scoreboard: Optional[TraderScoreboard] = None,
        selective_filter: Optional[SelectiveCopyFilter] = None,
    ):
        self.wallets = wallets or getattr(Config, "COPY_WALLETS", [])
        self.poll_interval = poll_interval or getattr(Config, "COPY_POLL_INTERVAL", 5)

        # HTTP session with connection pooling
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "AcropolisBot/2.0",
            "Accept": "application/json",
        })
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=5,
            pool_maxsize=10,
            max_retries=2,
        )
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # State
        self.last_seen: dict[str, int] = {w: int(time.time()) for w in self.wallets}
        self.scoreboard = scoreboard or TraderScoreboard(self.wallets)
        self.selective_filter = selective_filter

        # Stats
        self.polls: int = 0
        self._triggered_polls: int = 0
        self._poll_latencies: list[float] = []
        self._max_latency_history = 200

    # ── API calls ─────────────────────────────────────────────────────────

    def _fetch_activity(self, wallet: str, limit: int = 10) -> list[dict]:
        """Fetch recent activity for a wallet from Polymarket API."""
        try:
            resp = self.session.get(
                f"{getattr(Config, 'DATA_API', 'https://data-api.polymarket.com')}/activity",
                params={"user": wallet, "limit": limit, "offset": 0},
                timeout=getattr(Config, "REST_TIMEOUT", 5),
            )
            resp.raise_for_status()
            data = resp.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    # ── parsing ───────────────────────────────────────────────────────────

    def _parse_market_slug(self, slug: str) -> Optional[tuple[MarketType, int]]:
        """Parse market slug → (MarketType, window_timestamp)."""
        match = self.MARKET_PATTERN.match(slug)
        if not match:
            return None

        asset = match.group(1).upper()
        interval = match.group(2).upper()
        timestamp = int(match.group(3))

        key = f"{asset}_{interval}"
        try:
            mt = MarketType[key]
            return mt, timestamp
        except KeyError:
            return None

    def _trade_to_signal(self, trade: dict, wallet: str) -> Optional[CopySignal]:
        """Convert a raw API trade dict to a CopySignal."""
        slug = trade.get("slug", "")
        parsed = self._parse_market_slug(slug)
        if not parsed:
            return None

        market_type, market_ts = parsed

        # Check if this market type is active
        active = getattr(Config, "ACTIVE_MARKETS", list(MarketType))
        if market_type not in active:
            return None

        direction = trade.get("outcome", "").lower()
        if direction not in ("up", "down"):
            return None

        side = trade.get("type", "BUY").upper()
        if side not in ("BUY", "SELL"):
            side = "BUY"

        trade_ts = trade.get("timestamp", 0)
        if isinstance(trade_ts, str):
            trade_ts = int(trade_ts)

        now_ms = int(time.time() * 1000)
        # trade_ts might be seconds or milliseconds
        ts_ms = trade_ts * 1000 if trade_ts < 2_000_000_000 else trade_ts
        delay_ms = max(0, now_ms - ts_ms)

        return CopySignal(
            wallet=wallet,
            direction=direction,
            side=side,
            market_ts=market_ts,
            trade_ts=trade_ts,
            price=float(trade.get("price", 0.5)),
            usdc_amount=float(trade.get("usdcSize", 0)),
            trader_name=trade.get("pseudonym", trade.get("name", wallet[:8])),
            market_type=market_type,
            delay_ms=delay_ms,
            market_slug=slug,
        )

    # ── polling ───────────────────────────────────────────────────────────

    def poll(self) -> list[CopySignal]:
        """Poll all tracked wallets for new trades.

        Returns new CopySignals since last poll.
        """
        poll_start = time.time()
        self.polls += 1
        signals: list[CopySignal] = []

        for wallet in self.wallets:
            # Skip blacklisted traders
            if self.scoreboard.is_blacklisted(wallet):
                continue

            activity = self._fetch_activity(wallet)
            last_ts = self.last_seen.get(wallet, 0)
            new_last_ts = last_ts

            for trade in activity:
                trade_ts = trade.get("timestamp", 0)
                if isinstance(trade_ts, str):
                    trade_ts = int(trade_ts)
                trade_type = trade.get("type", "")

                if trade_ts <= last_ts or trade_type != "TRADE":
                    continue

                sig = self._trade_to_signal(trade, wallet)
                if sig:
                    signals.append(sig)
                    new_last_ts = max(new_last_ts, trade_ts)

            self.last_seen[wallet] = new_last_ts

        # Track latency
        elapsed_ms = (time.time() - poll_start) * 1000
        self._poll_latencies.append(elapsed_ms)
        if len(self._poll_latencies) > self._max_latency_history:
            self._poll_latencies = self._poll_latencies[-self._max_latency_history:]

        return signals

    def trigger_immediate_poll(self, market_slug: str = "") -> list[CopySignal]:
        """Trigger an immediate poll (called by WebSocket on trade event).

        Returns signals found, bypassing the normal interval timer.
        """
        self._triggered_polls += 1
        return self.poll()

    def get_latest_btc_5m_trades(self, wallet: str, limit: int = 3) -> list[CopySignal]:
        """Fetch recent BTC 5-min trades for a specific wallet (no state mutation)."""
        activity = self._fetch_activity(wallet, limit=limit * 3)  # over-fetch to filter
        signals = []
        for trade in activity:
            sig = self._trade_to_signal(trade, wallet)
            if sig and sig.market_type == MarketType.BTC_5M:
                signals.append(sig)
                if len(signals) >= limit:
                    break
        return signals

    # ── stats ─────────────────────────────────────────────────────────────

    @property
    def avg_poll_latency_ms(self) -> float:
        if not self._poll_latencies:
            return 0.0
        return sum(self._poll_latencies) / len(self._poll_latencies)

    def get_stats(self) -> dict:
        return {
            "wallets_tracked": len(self.wallets),
            "polls": self.polls,
            "triggered_polls": self._triggered_polls,
            "avg_poll_latency_ms": round(self.avg_poll_latency_ms, 1),
            "poll_interval": self.poll_interval,
            "scoreboard": self.scoreboard.get_stats(),
            "filter": self.selective_filter.get_stats() if self.selective_filter else None,
        }
