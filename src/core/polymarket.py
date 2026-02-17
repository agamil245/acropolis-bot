"""Polymarket API client with multi-market support, WebSocket streaming,
order book analysis, and comprehensive error handling.

Production-ready client adapted from reference bot with multi-asset support.
"""

import asyncio
import json
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import websockets
    from websockets.exceptions import ConnectionClosed
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

from src.config import Config, MarketType


# ═══════════════════════════════════════════════════════════════════════════════
# DELAY IMPACT MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class DelayImpactModel:
    """Non-linear, liquidity-aware model for copy delay price impact.

    Calculates the expected price impact from copying a trade with delay.
    Uses: impact = sqrt(delay) * base_coef * liquidity_factor * volatility_factor

    This is critical for copytrade simulation accuracy — the longer we wait
    to copy a trade, the worse our fill price will be.
    """

    base_coef: float = field(default_factory=lambda: Config.DELAY_MODEL_BASE_COEF)
    max_impact: float = field(default_factory=lambda: Config.DELAY_MODEL_MAX_IMPACT)
    baseline_spread: float = field(default_factory=lambda: Config.DELAY_MODEL_BASELINE_SPREAD)

    def calculate_impact(
        self,
        delay_ms: int,
        order_size: float = 0.0,
        depth_at_best: float = 0.0,
        spread: float = 0.0,
        side: str = "BUY",
    ) -> tuple[float, dict]:
        """Calculate delay impact percentage.

        Args:
            delay_ms: Milliseconds since the original trade
            order_size: Our order size in USD
            depth_at_best: Available liquidity at best price level
            spread: Current bid-ask spread
            side: "BUY" or "SELL"

        Returns:
            Tuple of (impact_pct, breakdown_dict)
            - impact_pct: Expected price impact as percentage (e.g., 1.5 = 1.5%)
            - breakdown_dict: Detailed calculation breakdown for logging
        """
        if delay_ms <= 0:
            return 0.0, {"delay_ms": 0, "impact_pct": 0.0}

        delay_seconds = delay_ms / 1000.0

        # Base impact: sqrt decay - faster initial impact, slower growth over time
        # sqrt(1s) * 0.8 = 0.8%, sqrt(4s) * 0.8 = 1.6%, sqrt(9s) * 0.8 = 2.4%
        base_impact = math.sqrt(delay_seconds) * self.base_coef

        # Liquidity factor: larger orders relative to available depth = more impact
        if depth_at_best > 0 and order_size > 0:
            liq_ratio = order_size / (depth_at_best * 0.5)
            liq_factor = min(2.0, max(0.5, liq_ratio))
        else:
            liq_factor = 1.0

        # Volatility factor: wider spread = more volatile = more impact
        if spread > 0 and self.baseline_spread > 0:
            vol_ratio = spread / self.baseline_spread
            vol_factor = min(2.0, max(0.5, vol_ratio))
        else:
            vol_factor = 1.0

        # Final impact with cap
        final_impact = base_impact * liq_factor * vol_factor
        final_impact = min(self.max_impact, final_impact)

        breakdown = {
            "delay_ms": delay_ms,
            "delay_seconds": round(delay_seconds, 2),
            "base_impact": round(base_impact, 4),
            "liquidity_factor": round(liq_factor, 2),
            "volatility_factor": round(vol_factor, 2),
            "final_impact_pct": round(final_impact, 4),
            "order_size": round(order_size, 2),
            "depth_at_best": round(depth_at_best, 2),
            "spread": round(spread, 4),
        }

        return final_impact, breakdown


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET DATA MODEL
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class Market:
    """A Polymarket prediction market (up/down binary)."""

    timestamp: int
    slug: str
    title: str
    closed: bool
    outcome: Optional[str]  # "up", "down", or None
    up_token_id: Optional[str]
    down_token_id: Optional[str]
    up_price: float
    down_price: float
    volume: float
    accepting_orders: bool
    taker_fee_bps: int = 1000  # Default 10% base fee
    resolved: bool = False
    market_type: MarketType = MarketType.BTC_5M

    @property
    def combined_price(self) -> float:
        """YES + NO price (should sum to ~1.0)."""
        return self.up_price + self.down_price

    @property
    def arbitrage_edge(self) -> float:
        """Arbitrage edge: how much less than 1.0 the combined price is."""
        return 1.0 - self.combined_price

    @property
    def is_arbitrage_opportunity(self) -> bool:
        """Check if combined price is below threshold."""
        return self.combined_price < Config.ARB_THRESHOLD

    @property
    def implied_direction(self) -> str:
        """Which direction the market is leaning."""
        if self.up_price > self.down_price:
            return "up"
        elif self.down_price > self.up_price:
            return "down"
        return "neutral"

    @property
    def market_bias_strength(self) -> float:
        """How strongly the market leans (0 = balanced, 0.5 = max)."""
        return abs(self.up_price - self.down_price) / 2

    @property
    def seconds_until_close(self) -> int:
        """Seconds until this market's window closes."""
        close_time = self.timestamp + self.market_type.interval_seconds
        return max(0, close_time - int(time.time()))

    @property
    def is_expired(self) -> bool:
        """Whether the market window has ended."""
        return int(time.time()) > self.timestamp + self.market_type.interval_seconds

    def get_price(self, direction: str) -> float:
        """Get price for a given direction."""
        return self.up_price if direction == "up" else self.down_price

    def get_token_id(self, direction: str) -> Optional[str]:
        """Get token ID for a given direction."""
        return self.up_token_id if direction == "up" else self.down_token_id


# ═══════════════════════════════════════════════════════════════════════════════
# ORDER BOOK MODELS
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class OrderBookLevel:
    """Single price level in the order book."""
    price: float
    size: float

    @property
    def value_usd(self) -> float:
        """USD value at this level."""
        return self.price * self.size


@dataclass
class CachedOrderBook:
    """Cached order book state with real-time WebSocket updates."""

    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0.0
    best_bid: float = 0.0
    best_ask: float = 0.0
    mid: float = 0.5

    def update_from_snapshot(self, data: dict):
        """Update from full orderbook snapshot."""
        self.bids = [
            OrderBookLevel(float(b["price"]), float(b["size"]))
            for b in data.get("bids", [])
        ]
        self.asks = [
            OrderBookLevel(float(a["price"]), float(a["size"]))
            for a in data.get("asks", [])
        ]
        self._recalculate()

    def update_from_delta(self, data: dict):
        """Update from orderbook delta (price_change event)."""
        changes = data.get("changes", [])
        for change in changes:
            side = change.get("side")
            price = float(change.get("price", 0))
            size = float(change.get("size", 0))

            if side == "BUY":
                self._update_level(self.bids, price, size, reverse=True)
            elif side == "SELL":
                self._update_level(self.asks, price, size, reverse=False)
        self._recalculate()

    def _update_level(self, levels: list[OrderBookLevel], price: float, size: float, reverse: bool):
        """Update a single price level."""
        for i, level in enumerate(levels):
            if abs(level.price - price) < 0.0001:
                if size == 0:
                    levels.pop(i)
                else:
                    level.size = size
                return
        if size > 0:
            levels.append(OrderBookLevel(price, size))
            levels.sort(key=lambda x: x.price, reverse=reverse)

    def _recalculate(self):
        """Recalculate best bid/ask and mid."""
        self.timestamp = time.time()
        if self.bids:
            self.bids.sort(key=lambda x: x.price, reverse=True)
            self.best_bid = self.bids[0].price
        if self.asks:
            self.asks.sort(key=lambda x: x.price)
            self.best_ask = self.asks[0].price
        if self.best_bid > 0 and self.best_ask > 0:
            self.mid = (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> float:
        """Bid-ask spread."""
        if self.best_ask > 0 and self.best_bid > 0:
            return self.best_ask - self.best_bid
        return 0.0

    @property
    def depth_at_best_bid(self) -> float:
        """USD depth at best bid."""
        return self.bids[0].value_usd if self.bids else 0.0

    @property
    def depth_at_best_ask(self) -> float:
        """USD depth at best ask."""
        return self.asks[0].value_usd if self.asks else 0.0

    @property
    def total_bid_depth(self) -> float:
        """Total USD depth on bid side."""
        return sum(level.value_usd for level in self.bids)

    @property
    def total_ask_depth(self) -> float:
        """Total USD depth on ask side."""
        return sum(level.value_usd for level in self.asks)

    @property
    def is_stale(self) -> bool:
        """Check if book data is older than 5 seconds."""
        return time.time() - self.timestamp > 5.0

    def get_execution_price(self, side: str, amount_usd: float) -> tuple[float, float, float]:
        """Calculate execution price by walking the book.

        Returns: (execution_price, slippage_pct, fill_pct)
        """
        levels = self.asks if side == "BUY" else self.bids

        if not levels:
            return self.mid, 0.0, 0.0

        remaining = amount_usd
        total_shares = 0.0
        total_cost = 0.0

        for level in levels:
            if remaining <= 0:
                break
            level_value = level.price * level.size
            if level_value >= remaining:
                shares = remaining / level.price
                total_shares += shares
                total_cost += remaining
                remaining = 0
            else:
                total_shares += level.size
                total_cost += level_value
                remaining -= level_value

        if total_shares == 0:
            return self.mid, 0.0, 0.0

        exec_price = total_cost / total_shares
        filled_amount = amount_usd - remaining
        fill_pct = (filled_amount / amount_usd * 100) if amount_usd > 0 else 100.0

        # Slippage vs best price
        best_price = self.best_ask if side == "BUY" else self.best_bid
        if best_price > 0:
            slippage_pct = abs(exec_price - best_price) / best_price * 100
        else:
            slippage_pct = 0.0

        return exec_price, slippage_pct, fill_pct

    def to_dict(self) -> dict:
        """Convert to REST-API-compatible dict."""
        return {
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in self.bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in self.asks],
            "source": "websocket",
            "age_ms": int((time.time() - self.timestamp) * 1000),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET TRADE EVENT
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeEvent:
    """Real-time trade event from WebSocket."""
    token_id: str
    market_id: str
    price: float
    size: float
    side: str  # "BUY" or "SELL"
    timestamp: float  # unix seconds
    taker_address: str = ""
    maker_address: str = ""


# ═══════════════════════════════════════════════════════════════════════════════
# POLYMARKET REST CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class PolymarketClient:
    """Multi-market Polymarket API client with connection pooling, caching,
    rate limiting, and comprehensive order book analysis.

    Features:
    - Connection pooling for HTTP performance
    - Configurable timeouts and automatic retries
    - Token ID caching (never change for a given market)
    - Market data caching with TTL-based invalidation
    - Order book walkthrough for execution price estimation
    - Delay impact modeling for copytrade simulation
    - Fee calculation using Polymarket's formula
    - Batch operations for pre-fetching
    """

    def __init__(self, timeout: Optional[float] = None, use_cache: bool = True):
        self.gamma = Config.GAMMA_API
        self.clob = Config.CLOB_API
        self.data_api = Config.DATA_API
        self.timeout = timeout or Config.REST_TIMEOUT

        # Create session with connection pooling
        self.session = requests.Session()

        # Add proxy if configured
        if Config.PROXY_URL:
            self.session.proxies = {
                "http": Config.PROXY_URL,
                "https": Config.PROXY_URL,
            }

        # Configure retry strategy
        retry_strategy = Retry(
            total=Config.REST_RETRIES,
            backoff_factor=0.1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )

        adapter = HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=retry_strategy,
        )
        self.session.mount("https://", adapter)
        self.session.headers.update({
            "User-Agent": "AcropolisBot/2.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })

        # Caches
        self._token_cache: dict[tuple[str, int], tuple[Optional[str], Optional[str]]] = {}
        self._market_cache: dict[tuple[str, int], Market] = {}
        self._use_cache = use_cache

        # Rate limiting
        self._request_times: list[float] = []
        self._rate_limit = Config.RATE_LIMIT_REQUESTS_PER_MINUTE

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits. Returns True if OK."""
        now = time.time()
        cutoff = now - 60
        self._request_times = [t for t in self._request_times if t > cutoff]
        if len(self._request_times) >= self._rate_limit:
            return False
        self._request_times.append(now)
        return True

    # ─── Market Fetching ──────────────────────────────────────────────────

    def get_market(
        self,
        market_type_or_ts: "MarketType | int",
        timestamp: Optional[int] = None,
        use_cache: bool = True,
    ) -> Optional[Market]:
        """Fetch a market by type and timestamp.

        Supports two calling conventions:
          get_market(MarketType.BTC_5M, 1234567890)
          get_market(1234567890)  # assumes BTC_5M for backward compat
        """
        if isinstance(market_type_or_ts, int) and timestamp is None:
            # Backward-compatible: just a timestamp, assume BTC_5M
            timestamp = market_type_or_ts
            market_type = MarketType.BTC_5M
        elif isinstance(market_type_or_ts, MarketType):
            market_type = market_type_or_ts
            if timestamp is None:
                raise ValueError("timestamp required when market_type is provided")
        else:
            raise ValueError(f"Invalid first argument: {market_type_or_ts}")

        cache_key = (market_type.value, timestamp)

        # Check cache
        if use_cache and self._use_cache and cache_key in self._market_cache:
            cached = self._market_cache[cache_key]
            now = int(time.time())
            market_end = timestamp + market_type.interval_seconds

            if cached.closed and cached.outcome:
                return cached  # Resolved markets are final
            elif now < market_end:
                return cached  # Still in window, cache fresh enough

        slug = f"{market_type.value}-{timestamp}"

        if not self._check_rate_limit():
            time.sleep(0.5)

        try:
            resp = self.session.get(
                f"{self.gamma}/events",
                params={"slug": slug},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data:
                return None

            event = data[0]
            markets = event.get("markets", [])
            if not markets:
                return None

            m = markets[0]

            # Parse token IDs
            token_ids = json.loads(m.get("clobTokenIds", "[]"))
            up_token = token_ids[0] if len(token_ids) > 0 else None
            down_token = token_ids[1] if len(token_ids) > 1 else None
            self._token_cache[cache_key] = (up_token, down_token)

            # Parse prices
            prices = json.loads(m.get("outcomePrices", "[0.5, 0.5]"))
            up_price = float(prices[0]) if prices else 0.5
            down_price = float(prices[1]) if len(prices) > 1 else 0.5

            # Determine outcome if resolved
            outcome = None
            is_closed = m.get("closed", False)
            uma_status = m.get("umaResolutionStatus", "")
            is_resolved = uma_status == "resolved"

            if is_closed and (is_resolved or up_price > 0.99 or down_price > 0.99):
                if up_price > 0.99:
                    outcome = "up"
                elif down_price > 0.99:
                    outcome = "down"

            # Fee rate
            taker_fee_bps = m.get("takerBaseFee")
            if taker_fee_bps is None:
                taker_fee_bps = 1000
            else:
                taker_fee_bps = int(taker_fee_bps)

            market = Market(
                timestamp=timestamp,
                slug=slug,
                title=event.get("title", ""),
                closed=event.get("closed", False) or m.get("closed", False),
                outcome=outcome,
                up_token_id=up_token,
                down_token_id=down_token,
                up_price=up_price,
                down_price=down_price,
                volume=event.get("volume", 0),
                accepting_orders=m.get("acceptingOrders", False),
                taker_fee_bps=taker_fee_bps,
                resolved=is_resolved,
                market_type=market_type,
            )

            if self._use_cache:
                self._market_cache[cache_key] = market

            return market

        except requests.exceptions.Timeout:
            return None
        except Exception as e:
            print(f"[polymarket] Error fetching {slug}: {e}")
            return None

    def get_token_ids(self, market_type: MarketType, timestamp: int) -> tuple[Optional[str], Optional[str]]:
        """Get cached token IDs for a market, fetching if needed."""
        cache_key = (market_type.value, timestamp)
        if cache_key in self._token_cache:
            return self._token_cache[cache_key]
        market = self.get_market(market_type, timestamp)
        if market:
            return (market.up_token_id, market.down_token_id)
        return (None, None)

    def get_all_active_markets(self) -> list[Market]:
        """Get all active (non-closed) markets for configured market types."""
        markets = []
        now = int(time.time())

        for market_type in Config.ACTIVE_MARKETS:
            interval = market_type.interval_seconds
            current_window = (now // interval) * interval

            for offset in [0, interval]:
                ts = current_window + offset
                market = self.get_market(market_type, ts)
                if market and not market.closed:
                    markets.append(market)

        return markets

    def prefetch_markets(self, timestamps: list[int], market_type: MarketType = MarketType.BTC_5M) -> int:
        """Pre-fetch and cache multiple markets. Returns count of successful fetches."""
        success = 0
        for ts in timestamps:
            if self.get_market(market_type, ts) is not None:
                success += 1
        return success

    def get_upcoming_market_timestamps(self, market_type: MarketType = MarketType.BTC_5M, count: int = 5) -> list[int]:
        """Get timestamps of upcoming market windows."""
        now = int(time.time())
        interval = market_type.interval_seconds
        current_window = (now // interval) * interval
        return [current_window + (i * interval) for i in range(count)]

    def get_next_market_timestamp(self, market_type: MarketType = MarketType.BTC_5M) -> int:
        """Get timestamp of the next upcoming window."""
        now = int(time.time())
        interval = market_type.interval_seconds
        current_window = (now // interval) * interval
        next_window = current_window + interval
        if now - current_window < 60:
            return current_window
        return next_window

    # ─── Outcomes ─────────────────────────────────────────────────────────

    def get_recent_outcomes(self, market_type: MarketType, count: int = 10) -> list[str]:
        """Get the last N resolved market outcomes (oldest first)."""
        now = int(time.time())
        interval = market_type.interval_seconds
        current_window = (now // interval) * interval
        outcomes: list[str] = []

        ts = current_window - interval
        attempts = 0
        max_attempts = count + 10

        while len(outcomes) < count and attempts < max_attempts:
            market = self.get_market(market_type, ts)
            if market and market.closed and market.outcome:
                outcomes.append(market.outcome)
            ts -= interval
            attempts += 1
            time.sleep(0.05)

        outcomes.reverse()
        return outcomes

    # ─── Order Book ───────────────────────────────────────────────────────

    def get_orderbook(self, token_id: str) -> dict:
        """Get order book for a token."""
        if not self._check_rate_limit():
            time.sleep(0.5)
        try:
            resp = self.session.get(
                f"{self.clob}/book",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            return {}
        except Exception as e:
            print(f"[polymarket] Error fetching orderbook: {e}")
            return {}

    def get_orderbooks(self, token_ids: list[str]) -> dict[str, dict]:
        """Get multiple order books (attempts batch, falls back to individual)."""
        try:
            resp = self.session.get(
                f"{self.clob}/books",
                params={"token_ids": ",".join(token_ids)},
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        # Fallback
        results = {}
        for tid in token_ids:
            book = self.get_orderbook(tid)
            if book:
                results[tid] = book
        return results

    def get_midpoint(self, token_id: str) -> Optional[float]:
        """Get midpoint price for a token (faster than full orderbook)."""
        try:
            resp = self.session.get(
                f"{self.clob}/midpoint",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("mid", 0.5))
        except Exception:
            return None

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get best price for a token (fastest endpoint)."""
        try:
            resp = self.session.get(
                f"{self.clob}/price",
                params={"token_id": token_id, "side": side},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0.5))
        except Exception:
            return None

    def get_spread(self, token_id: str) -> Optional[tuple[float, float]]:
        """Get bid-ask spread. Returns (best_bid, best_ask) or None."""
        try:
            resp = self.session.get(
                f"{self.clob}/spread",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return (float(data.get("bid", 0)), float(data.get("ask", 0)))
        except Exception:
            return None

    def get_fee_rate(self, token_id: str) -> int:
        """Get fee rate in basis points for a token."""
        DEFAULT_FEE_BPS = 1000
        try:
            resp = self.session.get(
                f"{self.clob}/fee-rate",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            return int(data.get("base_fee", DEFAULT_FEE_BPS))
        except Exception:
            return DEFAULT_FEE_BPS

    # ─── Execution Price Estimation ───────────────────────────────────────

    @staticmethod
    def calculate_fee(price: float, base_fee_bps: int) -> float:
        """Calculate actual fee percentage from price and base fee.

        Polymarket fee formula: fee = price * (1 - price) * base_fee / 10000
        At 50¢ with base_fee=1000: 0.50 * 0.50 * 0.10 = 2.5%
        At 80¢ with base_fee=1000: 0.80 * 0.20 * 0.10 = 1.6%
        """
        if base_fee_bps == 0:
            return 0.0
        return price * (1 - price) * base_fee_bps / 10000

    def get_execution_price(
        self,
        token_id: str,
        side: str,
        amount_usd: float,
        copy_delay_ms: int = 0,
    ) -> tuple[float, float, float, float, float, Optional[dict]]:
        """Calculate execution price with slippage and optional delay impact.

        Args:
            token_id: The token to trade
            side: "BUY" or "SELL"
            amount_usd: Order size in USD
            copy_delay_ms: Milliseconds since original trade (copytrade)

        Returns:
            (execution_price, spread, slippage_pct, fill_pct, delay_impact_pct, delay_breakdown)
        """
        book = self.get_orderbook(token_id)
        if not book:
            return (0.5, 0.0, 0.0, 100.0, 0.0, None)

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return (0.5, 0.0, 0.0, 100.0, 0.0, None)

        asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
        bids_sorted = sorted(bids, key=lambda x: float(x["price"]), reverse=True)

        best_ask = float(asks_sorted[0]["price"])
        best_bid = float(bids_sorted[0]["price"])
        spread = best_ask - best_bid

        # Calculate depth at best level
        if side == "BUY":
            best_level = asks_sorted[0]
            depth_at_best = float(best_level["price"]) * float(best_level["size"])
            levels = asks_sorted
        else:
            best_level = bids_sorted[0]
            depth_at_best = float(best_level["price"]) * float(best_level["size"])
            levels = bids_sorted

        # Walk the book
        remaining_usd = amount_usd
        total_shares = 0.0
        total_cost = 0.0

        for level in levels:
            price = float(level["price"])
            size = float(level["size"])
            level_value = price * size

            if remaining_usd <= 0:
                break

            if level_value >= remaining_usd:
                shares_to_take = remaining_usd / price
                total_shares += shares_to_take
                total_cost += remaining_usd
                remaining_usd = 0
            else:
                total_shares += size
                total_cost += level_value
                remaining_usd -= level_value

        filled_amount = amount_usd - remaining_usd
        fill_pct = (filled_amount / amount_usd * 100) if amount_usd > 0 else 100.0

        if total_shares == 0:
            midpoint = (best_ask + best_bid) / 2
            return (midpoint, spread, 0.0, 0.0, 0.0, None)

        execution_price = total_cost / total_shares

        # Calculate slippage vs best price
        if side == "BUY":
            slippage_pct = (execution_price - best_ask) / best_ask * 100 if best_ask > 0 else 0
        else:
            slippage_pct = (best_bid - execution_price) / best_bid * 100 if best_bid > 0 else 0

        # Apply delay impact for copytrade
        delay_impact_pct = 0.0
        delay_breakdown = None

        if copy_delay_ms > 0:
            delay_model = DelayImpactModel()
            delay_impact_pct, delay_breakdown = delay_model.calculate_impact(
                delay_ms=copy_delay_ms,
                order_size=amount_usd,
                depth_at_best=depth_at_best,
                spread=spread,
                side=side,
            )
            if side == "BUY":
                execution_price *= 1 + delay_impact_pct / 100
            else:
                execution_price *= 1 - delay_impact_pct / 100
            execution_price = max(0.01, min(0.99, execution_price))

        return (
            execution_price,
            spread,
            max(0, slippage_pct),
            fill_pct,
            delay_impact_pct,
            delay_breakdown,
        )

    # ─── Limit Orders & Order Management ─────────────────────────────────

    def place_limit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
    ) -> Optional[dict]:
        """Place a limit order (GTC) via CLOB API.

        Args:
            token_id: Token to trade
            price: Limit price (0.01 - 0.99)
            size: Number of shares
            side: "BUY" or "SELL"

        Returns:
            Order response dict with orderID, or None on failure
        """
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            client = ClobClient(
                host=self.clob,
                key=Config.PRIVATE_KEY,
                chain_id=Config.CHAIN_ID,
                signature_type=Config.SIGNATURE_TYPE,
                funder=Config.FUNDER_ADDRESS if Config.SIGNATURE_TYPE == 1 else None,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            order_side = BUY if side.upper() == "BUY" else SELL
            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=order_side,
            )
            signed = client.create_order(order_args)
            resp = client.post_order(signed, OrderType.GTC)
            return resp
        except ImportError:
            print("[polymarket] py-clob-client not installed for limit orders")
            return None
        except Exception as e:
            print(f"[polymarket] Limit order failed: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host=self.clob,
                key=Config.PRIVATE_KEY,
                chain_id=Config.CHAIN_ID,
                signature_type=Config.SIGNATURE_TYPE,
                funder=Config.FUNDER_ADDRESS if Config.SIGNATURE_TYPE == 1 else None,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            client.cancel(order_id)
            return True
        except Exception as e:
            print(f"[polymarket] Cancel order failed: {e}")
            return False

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host=self.clob,
                key=Config.PRIVATE_KEY,
                chain_id=Config.CHAIN_ID,
                signature_type=Config.SIGNATURE_TYPE,
                funder=Config.FUNDER_ADDRESS if Config.SIGNATURE_TYPE == 1 else None,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            client.cancel_all()
            return True
        except Exception as e:
            print(f"[polymarket] Cancel all failed: {e}")
            return False

    def get_open_orders(self, market_id: Optional[str] = None) -> list[dict]:
        """Get all open orders, optionally filtered by market."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host=self.clob,
                key=Config.PRIVATE_KEY,
                chain_id=Config.CHAIN_ID,
                signature_type=Config.SIGNATURE_TYPE,
                funder=Config.FUNDER_ADDRESS if Config.SIGNATURE_TYPE == 1 else None,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            if market_id:
                orders = client.get_orders(market=market_id)
            else:
                orders = client.get_orders()
            return orders if isinstance(orders, list) else []
        except Exception as e:
            print(f"[polymarket] Get open orders failed: {e}")
            return []

    def get_order_status(self, order_id: str) -> Optional[dict]:
        """Get status of a specific order."""
        try:
            from py_clob_client.client import ClobClient
            client = ClobClient(
                host=self.clob,
                key=Config.PRIVATE_KEY,
                chain_id=Config.CHAIN_ID,
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            return client.get_order(order_id)
        except Exception as e:
            print(f"[polymarket] Get order status failed: {e}")
            return None

    # ─── Wallet Activity (for copytrade) ──────────────────────────────────

    def get_wallet_trades(self, wallet: str, limit: int = 10) -> list[dict]:
        """Get recent trades for a wallet address from the data API."""
        try:
            resp = self.session.get(
                f"{self.data_api}/activity",
                params={"address": wallet, "limit": limit},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json() or []
        except Exception as e:
            print(f"[polymarket] Error fetching wallet trades: {e}")
            return []


# ═══════════════════════════════════════════════════════════════════════════════
# WEBSOCKET CLIENT
# ═══════════════════════════════════════════════════════════════════════════════

class PolymarketWebSocket:
    """WebSocket client for real-time Polymarket data feeds.

    Provides ~100ms latency for orderbook updates vs ~1s for REST API.

    Supports:
    - Order book subscriptions with snapshot + delta updates
    - Real-time trade event streaming
    - Automatic reconnection with exponential backoff
    - Thread-safe access to cached orderbooks
    """

    def __init__(self, on_trade: Optional[Callable[[TradeEvent], None]] = None):
        if not HAS_WEBSOCKETS:
            raise ImportError("websockets package required: pip install websockets")

        self._on_trade = on_trade
        self._orderbooks: dict[str, CachedOrderBook] = {}
        self._subscribed_tokens: set[str] = set()
        self._subscribed_markets: set[str] = set()
        self._ws = None
        self._running = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._connected = threading.Event()
        self._lock = threading.Lock()

        # Stats
        self.reconnect_count = 0
        self.last_message_time = 0.0
        self.messages_received = 0

    def start(self):
        """Start WebSocket connection in background thread."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Wait for connection
        self._connected.wait(timeout=5.0)

    def stop(self):
        """Stop WebSocket connection gracefully."""
        self._running = False
        if self._loop and self._loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(self._graceful_shutdown(), self._loop)
                time.sleep(0.5)
                self._loop.call_soon_threadsafe(self._loop.stop)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)

    async def _graceful_shutdown(self):
        """Close WebSocket and cancel tasks."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        tasks = [t for t in asyncio.all_tasks(self._loop) if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _run_loop(self):
        """Run asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        except Exception as e:
            if self._running:
                print(f"[ws] Event loop error: {e}")
        finally:
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self._loop.close()

    async def _connect_loop(self):
        """Main connection loop with reconnection."""
        while self._running:
            try:
                async with websockets.connect(
                    Config.WS_CLOB_URL,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected.set()
                    print(f"[ws] Connected to {Config.WS_CLOB_URL}")

                    await self._resubscribe()

                    async for message in ws:
                        self.last_message_time = time.time()
                        self.messages_received += 1
                        raw = message.decode("utf-8", errors="ignore") if isinstance(message, bytes) else message
                        await self._handle_message(raw)

            except Exception as e:
                if self._running:
                    print(f"[ws] Connection error: {e}")
                self._connected.clear()

            if self._running:
                self.reconnect_count += 1
                wait_time = min(30, 2 ** min(self.reconnect_count, 5))
                print(f"[ws] Reconnecting in {wait_time}s...")
                await asyncio.sleep(wait_time)

    async def _resubscribe(self):
        """Resubscribe after reconnect."""
        for market_id in self._subscribed_markets:
            await self._send_subscribe(market_id)

    async def _send_subscribe(self, market_id: str):
        """Send subscription message."""
        if not self._ws:
            return
        msg = {"type": "subscribe", "channel": "market", "market": market_id}
        await self._ws.send(json.dumps(msg))

    async def _handle_message(self, raw: str):
        """Handle incoming WebSocket message."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", data.get("event_type", ""))

        if msg_type == "book":
            token_id = data.get("asset_id", "")
            if token_id:
                with self._lock:
                    if token_id not in self._orderbooks:
                        self._orderbooks[token_id] = CachedOrderBook(token_id=token_id)
                    self._orderbooks[token_id].update_from_snapshot(data)

        elif msg_type == "price_change":
            token_id = data.get("asset_id", "")
            if token_id and token_id in self._orderbooks:
                with self._lock:
                    self._orderbooks[token_id].update_from_delta(data)

        elif msg_type == "last_trade_price":
            token_id = data.get("asset_id", "")
            market_id = data.get("market", "")
            trade = TradeEvent(
                token_id=token_id,
                market_id=market_id,
                price=float(data.get("price", 0)),
                size=float(data.get("size", 0)),
                side=data.get("side", "BUY"),
                timestamp=float(data.get("timestamp", time.time())),
            )
            if self._on_trade:
                self._on_trade(trade)

    def subscribe_market(self, condition_id: str, token_ids: Optional[list[str]] = None):
        """Subscribe to a market's orderbook and trade updates."""
        self._subscribed_markets.add(condition_id)
        if token_ids:
            for tid in token_ids:
                self._subscribed_tokens.add(tid)
                with self._lock:
                    if tid not in self._orderbooks:
                        self._orderbooks[tid] = CachedOrderBook(token_id=tid)
        if self._loop and self._connected.is_set():
            asyncio.run_coroutine_threadsafe(self._send_subscribe(condition_id), self._loop)

    def unsubscribe_market(self, condition_id: str):
        """Unsubscribe from a market."""
        self._subscribed_markets.discard(condition_id)
        if self._loop and self._ws:
            msg = {"type": "unsubscribe", "channel": "market", "market": condition_id}
            asyncio.run_coroutine_threadsafe(self._ws.send(json.dumps(msg)), self._loop)

    def get_orderbook(self, token_id: str) -> Optional[CachedOrderBook]:
        """Get cached orderbook for a token."""
        with self._lock:
            return self._orderbooks.get(token_id)

    def get_execution_price(
        self, token_id: str, side: str, amount_usd: float, copy_delay_ms: int = 0
    ) -> tuple[float, float, float, float, float, Optional[dict]]:
        """Get execution price from cached orderbook with optional delay impact.

        Returns: (exec_price, spread, slippage_pct, fill_pct, delay_impact_pct, delay_breakdown)
        """
        book = self.get_orderbook(token_id)

        if book and book.timestamp > 0:
            exec_price, slippage_pct, fill_pct = book.get_execution_price(side, amount_usd)
            spread = book.spread

            depth_at_best = book.depth_at_best_ask if side == "BUY" else book.depth_at_best_bid

            delay_impact_pct = 0.0
            delay_breakdown = None

            if copy_delay_ms > 0:
                delay_model = DelayImpactModel()
                delay_impact_pct, delay_breakdown = delay_model.calculate_impact(
                    delay_ms=copy_delay_ms,
                    order_size=amount_usd,
                    depth_at_best=depth_at_best,
                    spread=spread,
                    side=side,
                )
                if side == "BUY":
                    exec_price *= 1 + delay_impact_pct / 100
                else:
                    exec_price *= 1 - delay_impact_pct / 100
                exec_price = max(0.01, min(0.99, exec_price))

            return (exec_price, spread, slippage_pct, fill_pct, delay_impact_pct, delay_breakdown)

        return 0.5, 0.0, 0.0, 100.0, 0.0, None

    def get_mid(self, token_id: str) -> Optional[float]:
        """Get midpoint price from cached orderbook."""
        book = self.get_orderbook(token_id)
        if book and book.timestamp > 0:
            return book.mid
        return None

    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def stats(self) -> dict:
        return {
            "connected": self.is_connected(),
            "reconnect_count": self.reconnect_count,
            "messages_received": self.messages_received,
            "last_message_age": time.time() - self.last_message_time if self.last_message_time else None,
            "subscribed_markets": len(self._subscribed_markets),
            "cached_orderbooks": len(self._orderbooks),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET DATA CACHE (combines WebSocket + REST)
# ═══════════════════════════════════════════════════════════════════════════════

class MarketDataCache:
    """High-level cache for market data combining WebSocket feeds with REST fallback.

    Provides a unified interface for getting orderbooks and execution prices,
    automatically choosing the fastest available data source.
    """

    def __init__(self, use_websocket: bool = True):
        self._rest_client = PolymarketClient()
        self._ws: Optional[PolymarketWebSocket] = None
        self._use_websocket = use_websocket

        self._token_cache: dict[int, tuple[str, str]] = {}
        self._trade_callbacks: list[Callable[[TradeEvent], None]] = []

        if use_websocket and HAS_WEBSOCKETS:
            self._ws = PolymarketWebSocket(on_trade=self._handle_trade)

    def start(self):
        """Start data feeds."""
        if self._ws:
            self._ws.start()
            print("[cache] WebSocket started")

    def stop(self):
        """Stop data feeds."""
        if self._ws:
            self._ws.stop()
            print("[cache] WebSocket stopped")

    def _handle_trade(self, trade: TradeEvent):
        """Dispatch trade events to registered callbacks."""
        for cb in self._trade_callbacks:
            try:
                cb(trade)
            except Exception as e:
                print(f"[cache] Trade callback error: {e}")

    def on_trade(self, callback: Callable[[TradeEvent], None]):
        """Register a trade callback."""
        self._trade_callbacks.append(callback)

    def prefetch_markets(self, timestamps: list[int], market_type: MarketType = MarketType.BTC_5M):
        """Pre-fetch and cache market data, subscribing to WebSocket if available."""
        for ts in timestamps:
            market = self._rest_client.get_market(market_type, ts)
            if market and market.up_token_id and market.down_token_id:
                self._token_cache[ts] = (market.up_token_id, market.down_token_id)
                if self._ws and self._ws.is_connected():
                    token_ids = [t for t in (market.up_token_id, market.down_token_id) if t]
                    self._ws.subscribe_market(market.slug, token_ids)

    def get_orderbook(self, token_id: str) -> dict:
        """Get orderbook from WebSocket cache or REST fallback."""
        if self._ws and self._ws.is_connected():
            book = self._ws.get_orderbook(token_id)
            if book and not book.is_stale:
                return book.to_dict()

        book = self._rest_client.get_orderbook(token_id)
        if book:
            book["source"] = "rest"
        return book

    def get_execution_price(
        self, token_id: str, side: str, amount_usd: float, copy_delay_ms: int = 0
    ) -> tuple[float, float, float, float, float, Optional[dict]]:
        """Get execution price from best available source."""
        if self._ws and self._ws.is_connected():
            book = self._ws.get_orderbook(token_id)
            if book and time.time() - book.timestamp < 2:
                return self._ws.get_execution_price(token_id, side, amount_usd, copy_delay_ms)

        return self._rest_client.get_execution_price(token_id, side, amount_usd, copy_delay_ms)

    def get_mid(self, token_id: str) -> Optional[float]:
        """Get midpoint from best available source."""
        if self._ws and self._ws.is_connected():
            mid = self._ws.get_mid(token_id)
            if mid is not None:
                return mid
        return self._rest_client.get_midpoint(token_id)

    @property
    def ws_connected(self) -> bool:
        return self._ws.is_connected() if self._ws else False

    @property
    def stats(self) -> dict:
        stats = {
            "cached_markets": len(self._token_cache),
            "use_websocket": self._use_websocket,
        }
        if self._ws:
            stats["websocket"] = self._ws.stats
        return stats
