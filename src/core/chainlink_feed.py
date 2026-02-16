"""Chainlink Oracle Price Feed — Read the settlement source directly.

Polymarket 5-min crypto markets resolve using Chainlink oracle data on Polygon.
There's a ~1 minute delay between Chainlink updating and Polymarket repricing.
By reading Chainlink directly, we know the settlement answer BEFORE the market adjusts.

This is THE edge. Chainlink IS the oracle. We're reading the answer sheet.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

from src.config import Config


# Chainlink AggregatorV3Interface — minimal ABI for latestRoundData()
AGGREGATOR_V3_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"internalType": "uint80", "name": "roundId", "type": "uint80"},
            {"internalType": "int256", "name": "answer", "type": "int256"},
            {"internalType": "uint256", "name": "startedAt", "type": "uint256"},
            {"internalType": "uint256", "name": "updatedAt", "type": "uint256"},
            {"internalType": "uint80", "name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"internalType": "uint8", "name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
]

# Chainlink price feed addresses on Polygon mainnet
CHAINLINK_FEEDS = {
    "BTC": "0xc907E116054Ad103354f2D350FD2514433D57F6f",
    "ETH": "0xF9680D99D6C9589e2a93a78A04A279e509205945",
    "SOL": "0x4FFb93D4D61b7e2FDA23b2d4f5dB32c9aDA86AA0",
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class ChainlinkPrice:
    """A single Chainlink price reading."""
    asset: str
    price: float
    round_id: int
    updated_at: int          # unix timestamp from the contract
    read_at: float           # time.time() when we read it
    decimals: int = 8

    @property
    def age_seconds(self) -> float:
        """How old this reading is (wall clock)."""
        return time.time() - self.read_at

    @property
    def chain_age_seconds(self) -> float:
        """How old the on-chain update is."""
        return time.time() - self.updated_at


@dataclass
class MomentumReading:
    """Momentum within a 5-min window based on Chainlink data."""
    asset: str
    window_start_time: int       # unix timestamp of window start
    window_start_price: float    # Chainlink price at window start
    current_price: float         # latest Chainlink price
    direction: str               # "up" or "down"
    change_pct: float            # percentage change from window start
    time_left_seconds: int       # seconds remaining in window
    confidence: float            # 0-1, how likely this direction holds

    @property
    def is_actionable(self) -> bool:
        """Whether this reading is worth trading on."""
        return (
            abs(self.change_pct) >= Config.CHAINLINK_MIN_MOMENTUM_PCT
            and self.time_left_seconds >= Config.CHAINLINK_MIN_TIME_LEFT
        )


@dataclass
class DivergenceSignal:
    """Signal when Chainlink implies a different price than Polymarket shows."""
    asset: str
    direction: str                # "up" or "down" — what Chainlink says
    chainlink_price: float        # current Chainlink price
    window_start_price: float     # price at start of 5-min window
    change_pct: float             # Chainlink % change in window
    polymarket_price: float       # current Polymarket price on the correct side
    implied_fair_value: float     # what the correct side SHOULD be trading at
    divergence: float             # implied_fair_value - polymarket_price
    time_left_seconds: int
    confidence: float             # 0-1
    recommended_action: str       # "buy_up", "buy_down", or "pass"

    @property
    def is_profitable(self) -> bool:
        return self.divergence >= Config.CHAINLINK_MIN_DIVERGENCE


# ═══════════════════════════════════════════════════════════════════════════════
# CHAINLINK PRICE FEED
# ═══════════════════════════════════════════════════════════════════════════════

class ChainlinkPriceFeed:
    """Read BTC/USD, ETH/USD, SOL/USD from Chainlink oracles on Polygon.

    Polls latestRoundData() every CHAINLINK_POLL_INTERVAL seconds.
    Tracks current price, last update timestamp, price changes.
    """

    def __init__(self, rpc_url: Optional[str] = None):
        self.rpc_url = rpc_url or Config.CHAINLINK_RPC_URL
        self._w3 = None
        self._contracts: dict[str, object] = {}
        self._latest: dict[str, ChainlinkPrice] = {}
        self._prev: dict[str, ChainlinkPrice] = {}
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._callbacks: list = []
        self._initialized = False

    def _init_web3(self):
        """Lazy-init web3 connection and contracts."""
        if self._initialized:
            return
        try:
            from web3 import Web3
            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 5}))
            if not self._w3.is_connected():
                print(f"[CHAINLINK] ⚠️ Cannot connect to {self.rpc_url}")
                return

            for asset, address in CHAINLINK_FEEDS.items():
                checksum = self._w3.to_checksum_address(address)
                self._contracts[asset] = self._w3.eth.contract(
                    address=checksum, abi=AGGREGATOR_V3_ABI
                )

            self._initialized = True
            print(f"[CHAINLINK] ✅ Connected to Polygon RPC, {len(self._contracts)} feeds")
        except ImportError:
            print("[CHAINLINK] ❌ web3 package required: pip install web3")
        except Exception as e:
            print(f"[CHAINLINK] ❌ Init error: {e}")

    def _read_price(self, asset: str) -> Optional[ChainlinkPrice]:
        """Read latest price from a Chainlink feed contract. Synchronous."""
        contract = self._contracts.get(asset)
        if not contract:
            return None
        try:
            result = contract.functions.latestRoundData().call()
            round_id, answer, started_at, updated_at, answered_in_round = result
            price = answer / 10**8  # 8 decimals
            return ChainlinkPrice(
                asset=asset,
                price=price,
                round_id=round_id,
                updated_at=updated_at,
                read_at=time.time(),
            )
        except Exception as e:
            print(f"[CHAINLINK] Error reading {asset}: {e}")
            return None

    def poll_all(self) -> dict[str, ChainlinkPrice]:
        """Poll all feeds once. Returns dict of asset -> ChainlinkPrice."""
        if not self._initialized:
            self._init_web3()
            if not self._initialized:
                return {}

        results = {}
        for asset in CHAINLINK_FEEDS:
            reading = self._read_price(asset)
            if reading:
                # Track previous
                if asset in self._latest:
                    self._prev[asset] = self._latest[asset]
                self._latest[asset] = reading
                results[asset] = reading
        return results

    async def start(self):
        """Start async polling loop."""
        if self._running:
            return
        self._init_web3()
        if not self._initialized:
            print("[CHAINLINK] Cannot start — web3 not initialized")
            return
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        print(f"[CHAINLINK] 🔄 Polling every {Config.CHAINLINK_POLL_INTERVAL}s")

    async def stop(self):
        """Stop polling."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        print("[CHAINLINK] Stopped")

    async def _poll_loop(self):
        """Main polling loop."""
        while self._running:
            try:
                # Run synchronous web3 call in executor to not block event loop
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self.poll_all)

                # Notify callbacks
                for cb in self._callbacks:
                    try:
                        cb(self._latest)
                    except Exception as e:
                        print(f"[CHAINLINK] Callback error: {e}")

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[CHAINLINK] Poll error: {e}")

            await asyncio.sleep(Config.CHAINLINK_POLL_INTERVAL)

    def on_update(self, callback):
        """Register callback for price updates. callback(dict[str, ChainlinkPrice])"""
        self._callbacks.append(callback)

    def get_price(self, asset: str) -> Optional[float]:
        """Get latest price for an asset."""
        reading = self._latest.get(asset)
        return reading.price if reading else None

    def get_reading(self, asset: str) -> Optional[ChainlinkPrice]:
        """Get full latest reading for an asset."""
        return self._latest.get(asset)

    def get_all_prices(self) -> dict[str, float]:
        """Get all latest prices."""
        return {a: r.price for a, r in self._latest.items()}

    def get_price_change(self, asset: str) -> Optional[float]:
        """Get price change since last poll."""
        curr = self._latest.get(asset)
        prev = self._prev.get(asset)
        if curr and prev and prev.price > 0:
            return ((curr.price - prev.price) / prev.price) * 100
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# CHAINLINK MOMENTUM DETECTOR
# ═══════════════════════════════════════════════════════════════════════════════

class ChainlinkMomentumDetector:
    """Track Chainlink price momentum within 5-minute windows.

    At the START of each 5-min window, records the Chainlink price.
    As the window progresses, tracks movement and calculates:
    - Direction (up/down from window start)
    - Confidence (how far from break-even)
    - Time left in window

    KEY INSIGHT: If Chainlink shows BTC up 0.5% from window start with 60s left,
    the UP side should be 70-80¢. If Polymarket UP is still at 50-55¢, free money.
    """

    def __init__(self, feed: ChainlinkPriceFeed, interval_seconds: int = 300):
        self.feed = feed
        self.interval = interval_seconds

        # Window start prices: asset -> (window_start_time, price)
        self._window_prices: dict[str, tuple[int, float]] = {}

    def _get_current_window(self) -> int:
        """Get the start timestamp of the current 5-min window."""
        now = int(time.time())
        return (now // self.interval) * self.interval

    def _ensure_window_price(self, asset: str) -> Optional[tuple[int, float]]:
        """Ensure we have the price at the start of the current window."""
        current_window = self._get_current_window()
        cached = self._window_prices.get(asset)

        if cached and cached[0] == current_window:
            return cached

        # New window — record current Chainlink price as the window start
        price = self.feed.get_price(asset)
        if price is None:
            return None

        entry = (current_window, price)
        self._window_prices[asset] = entry
        return entry

    def get_momentum(self, asset: str) -> Optional[MomentumReading]:
        """Get current momentum reading for an asset within the 5-min window."""
        window_data = self._ensure_window_price(asset)
        if not window_data:
            return None

        window_start, start_price = window_data
        current_price = self.feed.get_price(asset)
        if current_price is None or start_price == 0:
            return None

        now = int(time.time())
        time_left = max(0, (window_start + self.interval) - now)
        change_pct = ((current_price - start_price) / start_price) * 100.0
        direction = "up" if change_pct >= 0 else "down"

        # Confidence: based on magnitude of move and time remaining
        # Larger moves with less time left = higher confidence
        abs_change = abs(change_pct)
        time_factor = 1.0 - (time_left / self.interval)  # 0 at start, 1 at end
        confidence = min(0.95, abs_change * 2.0 * (0.5 + time_factor * 0.5))

        return MomentumReading(
            asset=asset,
            window_start_time=window_start,
            window_start_price=start_price,
            current_price=current_price,
            direction=direction,
            change_pct=change_pct,
            time_left_seconds=time_left,
            confidence=confidence,
        )

    def get_all_momentum(self) -> dict[str, MomentumReading]:
        """Get momentum for all tracked assets."""
        results = {}
        for asset in CHAINLINK_FEEDS:
            reading = self.get_momentum(asset)
            if reading:
                results[asset] = reading
        return results


# ═══════════════════════════════════════════════════════════════════════════════
# DIVERGENCE DETECTOR — Chainlink vs Polymarket
# ═══════════════════════════════════════════════════════════════════════════════

def estimate_fair_value(change_pct: float, time_left: int, interval: int = 300) -> float:
    """Estimate fair value of the 'correct' side based on Chainlink momentum.

    If BTC is up 0.5% with 60s left, what should the UP token be worth?

    Simple model:
    - change_pct > 0 → UP side; < 0 → DOWN side
    - Larger moves with less time → higher fair value (harder to reverse)
    - Base: 50¢ (coin flip) + momentum bonus

    Returns fair value 0.0 - 1.0 for the winning side.
    """
    if interval <= 0:
        return 0.5

    abs_change = abs(change_pct)
    time_elapsed_frac = 1.0 - (time_left / interval)

    # Momentum component: larger moves = more certain
    momentum_bonus = min(0.40, abs_change * 0.8)

    # Time decay: closer to end = more certain (less time to reverse)
    time_bonus = time_elapsed_frac * 0.15

    # Combined: start at 50¢, add bonuses
    fair = 0.50 + momentum_bonus + time_bonus

    return min(0.95, max(0.05, fair))


def get_divergence(
    asset: str,
    momentum: MomentumReading,
    polymarket_up_price: float,
    polymarket_down_price: float,
) -> DivergenceSignal:
    """Calculate divergence between Chainlink oracle and Polymarket pricing.

    This is the money function. Compares what Chainlink says the answer is
    vs what Polymarket is pricing it at. The gap is our edge.

    Args:
        asset: "BTC", "ETH", "SOL"
        momentum: Current ChainlinkMomentumDetector reading
        polymarket_up_price: Current price of UP token on Polymarket
        polymarket_down_price: Current price of DOWN token on Polymarket

    Returns:
        DivergenceSignal with direction, gap, and recommended action
    """
    direction = momentum.direction
    implied_fair = estimate_fair_value(
        momentum.change_pct, momentum.time_left_seconds
    )

    # The "correct" side based on Chainlink
    if direction == "up":
        poly_price = polymarket_up_price
        action = "buy_up"
    else:
        poly_price = polymarket_down_price
        action = "buy_down"

    divergence = implied_fair - poly_price

    # Only recommend action if divergence exceeds threshold
    if divergence < Config.CHAINLINK_MIN_DIVERGENCE:
        action = "pass"

    if momentum.time_left_seconds < Config.CHAINLINK_MIN_TIME_LEFT:
        action = "pass"

    return DivergenceSignal(
        asset=asset,
        direction=direction,
        chainlink_price=momentum.current_price,
        window_start_price=momentum.window_start_price,
        change_pct=momentum.change_pct,
        polymarket_price=poly_price,
        implied_fair_value=implied_fair,
        divergence=divergence,
        time_left_seconds=momentum.time_left_seconds,
        confidence=momentum.confidence,
        recommended_action=action,
    )
