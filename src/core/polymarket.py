"""Polymarket API client with multi-market support."""

import json
import time
from dataclasses import dataclass
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from src.config import Config, MarketType


@dataclass
class Market:
    """A Polymarket prediction market."""
    
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
    taker_fee_bps: int = 1000
    resolved: bool = False
    market_type: MarketType = MarketType.BTC_5M

    @property
    def combined_price(self) -> float:
        """Return YES + NO price."""
        return self.up_price + self.down_price

    @property
    def arbitrage_edge(self) -> float:
        """Return arbitrage edge (1.0 - combined_price)."""
        return 1.0 - self.combined_price

    @property
    def is_arbitrage_opportunity(self) -> bool:
        """Check if this market has an arbitrage opportunity."""
        return self.combined_price < Config.ARB_THRESHOLD


class PolymarketClient:
    """Multi-market Polymarket API client."""

    def __init__(self, timeout: Optional[float] = None):
        self.gamma = Config.GAMMA_API
        self.clob = Config.CLOB_API
        self.timeout = timeout or Config.REST_TIMEOUT

        # Create session with connection pooling
        self.session = requests.Session()

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
            "User-Agent": "AcropolisBot/1.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        })

        # Token cache: (market_type, timestamp) -> (up_token, down_token)
        self._token_cache: dict[tuple[MarketType, int], tuple[Optional[str], Optional[str]]] = {}
        self._market_cache: dict[tuple[MarketType, int], Market] = {}

    def get_market(
        self,
        market_type: MarketType,
        timestamp: int,
        use_cache: bool = True
    ) -> Optional[Market]:
        """Fetch a market by type and timestamp."""
        cache_key = (market_type, timestamp)
        
        # Check cache
        if use_cache and cache_key in self._market_cache:
            cached = self._market_cache[cache_key]
            now = int(time.time())
            market_end = timestamp + market_type.interval_seconds

            if cached.closed and cached.outcome:
                return cached
            elif now < market_end:
                return cached

        slug = f"{market_type.value}-{timestamp}"
        try:
            resp = self.session.get(
                f"{self.gamma}/events",
                params={"slug": slug},
                timeout=self.timeout
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

            # Determine outcome
            outcome = None
            is_closed = m.get("closed", False)
            uma_status = m.get("umaResolutionStatus", "")
            is_resolved = uma_status == "resolved"

            if is_closed and (is_resolved or up_price > 0.99 or down_price > 0.99):
                if up_price > 0.99:
                    outcome = "up"
                elif down_price > 0.99:
                    outcome = "down"

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
                market_type=market_type
            )

            self._market_cache[cache_key] = market
            return market

        except requests.exceptions.Timeout:
            return None
        except Exception as e:
            print(f"[polymarket] Error fetching {slug}: {e}")
            return None

    def get_all_active_markets(self) -> list[Market]:
        """Get all active markets for configured market types."""
        markets = []
        now = int(time.time())

        for market_type in Config.ACTIVE_MARKETS:
            interval = market_type.interval_seconds
            current_window = (now // interval) * interval
            
            # Get current and next window
            for offset in [0, interval]:
                ts = current_window + offset
                market = self.get_market(market_type, ts)
                if market and not market.closed:
                    markets.append(market)

        return markets

    def get_orderbook(self, token_id: str) -> dict:
        """Get order book for a token."""
        try:
            resp = self.session.get(
                f"{self.clob}/book",
                params={"token_id": token_id},
                timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {}

    def get_price(self, token_id: str, side: str = "BUY") -> Optional[float]:
        """Get best price for a token."""
        try:
            resp = self.session.get(
                f"{self.clob}/price",
                params={"token_id": token_id, "side": side},
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()
            return float(data.get("price", 0.5))
        except Exception:
            return None

    def get_execution_price(
        self,
        token_id: str,
        side: str,
        amount_usd: float
    ) -> tuple[float, float, float, float]:
        """
        Calculate execution price with slippage.
        
        Returns: (execution_price, spread, slippage_pct, fill_pct)
        """
        book = self.get_orderbook(token_id)
        if not book:
            return (0.5, 0.0, 0.0, 100.0)

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            return (0.5, 0.0, 0.0, 100.0)

        # Sort
        asks_sorted = sorted(asks, key=lambda x: float(x["price"]))
        bids_sorted = sorted(bids, key=lambda x: float(x["price"]), reverse=True)

        best_ask = float(asks_sorted[0]["price"])
        best_bid = float(bids_sorted[0]["price"])
        spread = best_ask - best_bid

        levels = asks_sorted if side == "BUY" else bids_sorted
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
            return (midpoint, spread, 0.0, 0.0)

        execution_price = total_cost / total_shares

        # Calculate slippage
        if side == "BUY":
            slippage_pct = (execution_price - best_ask) / best_ask * 100 if best_ask > 0 else 0
        else:
            slippage_pct = (best_bid - execution_price) / best_bid * 100 if best_bid > 0 else 0

        return (execution_price, spread, max(0, slippage_pct), fill_pct)

    def get_recent_outcomes(
        self,
        market_type: MarketType,
        count: int = 10
    ) -> list[str]:
        """Get recent resolved outcomes for a market type."""
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

    @staticmethod
    def calculate_fee(price: float, base_fee_bps: int) -> float:
        """Calculate fee percentage from price and base fee."""
        if base_fee_bps == 0:
            return 0.0
        return price * (1 - price) * base_fee_bps / 10000
