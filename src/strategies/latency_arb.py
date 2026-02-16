"""
Latency Arbitrage — Layer 2: The Sniper.

Connects to Binance WebSocket for real-time BTC/ETH/SOL price feeds.
When a sharp move is detected on Binance, checks if Polymarket 5-min
markets have repriced yet. If not, fires an aggressive market buy on
the correct side before the market catches up.

Inspired by 0x1d00's latency arb + gabagool22's directional overlay.

SPEED IS EVERYTHING — signal-to-order target: <10ms.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Callable, TYPE_CHECKING

from src.config import Config, MarketType

if TYPE_CHECKING:
    from src.core.polymarket import PolymarketClient, MarketDataCache, Market


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class BinanceTrade:
    """Single trade from Binance WebSocket."""
    symbol: str       # "BTCUSDT", "ETHUSDT", "SOLUSDT"
    price: float
    quantity: float
    timestamp_ms: int  # Binance event time
    is_buyer_maker: bool


@dataclass
class MomentumSignal:
    """Emitted when Binance shows sharp price movement."""
    asset: str                  # "BTC", "ETH", "SOL"
    direction: str              # "up" or "down"
    momentum_pct: float         # e.g., 0.25 = +0.25%
    window_seconds: float       # how long the window was
    binance_price: float        # current Binance price
    binance_price_start: float  # price at window start
    timestamp_ms: int
    strength: float = 0.0       # 0-1, how strong the signal is

    @property
    def market_type_5m(self) -> MarketType:
        mapping = {"BTC": MarketType.BTC_5M, "ETH": MarketType.ETH_5M, "SOL": MarketType.SOL_5M}
        return mapping.get(self.asset, MarketType.BTC_5M)


@dataclass
class LatencyArbSignal:
    """Full signal combining Binance momentum + Polymarket mispricing."""
    momentum: MomentumSignal
    polymarket_price: float     # current price on the "correct" side
    price_gap: float            # how much Polymarket is lagging
    recommended_size: float
    market: Optional["Market"] = None
    market_type: MarketType = MarketType.BTC_5M
    fired_at_ms: int = 0
    order_sent_at_ms: int = 0
    latency_ms: int = 0         # fired_at - momentum timestamp

    def __post_init__(self):
        if self.fired_at_ms == 0:
            self.fired_at_ms = int(time.time() * 1000)


@dataclass
class LatencyArbStats:
    """Cumulative statistics."""
    signals_detected: int = 0
    signals_fired: int = 0
    signals_skipped: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    avg_signal_latency_ms: float = 0.0
    avg_momentum_pct: float = 0.0
    avg_price_gap: float = 0.0
    total_volume: float = 0.0
    binance_messages: int = 0
    ws_reconnects: int = 0

    def to_dict(self) -> dict:
        total = self.wins + self.losses
        return {
            "signals_detected": self.signals_detected,
            "signals_fired": self.signals_fired,
            "signals_skipped": self.signals_skipped,
            "fire_rate": f"{self.signals_fired / self.signals_detected:.1%}" if self.signals_detected else "0%",
            "total_pnl": f"${self.total_pnl:+.2f}",
            "win_rate": f"{self.wins / total:.1%}" if total else "0%",
            "avg_signal_latency_ms": f"{self.avg_signal_latency_ms:.1f}",
            "avg_momentum_pct": f"{self.avg_momentum_pct:.3f}%",
            "avg_price_gap": f"{self.avg_price_gap:.4f}",
            "total_volume": f"${self.total_volume:.2f}",
            "binance_messages": self.binance_messages,
            "ws_reconnects": self.ws_reconnects,
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE PRICE TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

class BinancePriceTracker:
    """Ultra-low-latency price tracker using Binance trade stream.

    Maintains rolling windows of recent trades for momentum detection.
    """

    SYMBOLS = {
        "btcusdt": "BTC",
        "ethusdt": "ETH",
        "solusdt": "SOL",
    }

    def __init__(self, window_seconds: float = None):
        self.window_seconds = window_seconds or Config.MOMENTUM_WINDOW_SECONDS

        # Rolling trade buffers: symbol -> deque of (timestamp_ms, price)
        self._trades: dict[str, deque] = {
            asset: deque(maxlen=10000) for asset in self.SYMBOLS.values()
        }

        # Latest prices
        self._latest_price: dict[str, float] = {}
        self._latest_time: dict[str, int] = {}

    def on_trade(self, trade: BinanceTrade):
        """Process a Binance trade event. MUST BE FAST."""
        asset = self.SYMBOLS.get(trade.symbol.lower())
        if not asset:
            return

        self._trades[asset].append((trade.timestamp_ms, trade.price))
        self._latest_price[asset] = trade.price
        self._latest_time[asset] = trade.timestamp_ms

    def get_momentum(self, asset: str) -> Optional[tuple[float, float, float]]:
        """Calculate momentum for an asset over the rolling window.

        Returns: (momentum_pct, price_start, price_now) or None
        """
        trades = self._trades.get(asset)
        if not trades or len(trades) < 2:
            return None

        now_ms = trades[-1][0]
        cutoff_ms = now_ms - int(self.window_seconds * 1000)

        # Find earliest trade in window
        price_start = None
        for ts, price in trades:
            if ts >= cutoff_ms:
                price_start = price
                break

        if price_start is None or price_start == 0:
            return None

        price_now = trades[-1][1]
        momentum_pct = ((price_now - price_start) / price_start) * 100.0

        return momentum_pct, price_start, price_now

    def check_signals(self, threshold_pct: float = None) -> list[MomentumSignal]:
        """Check all assets for momentum signals exceeding threshold.

        Returns list of signals (usually 0 or 1).
        """
        threshold = threshold_pct or Config.MOMENTUM_THRESHOLD_PCT
        signals = []

        for asset in self.SYMBOLS.values():
            result = self.get_momentum(asset)
            if result is None:
                continue

            momentum_pct, price_start, price_now = result
            abs_momentum = abs(momentum_pct)

            if abs_momentum >= threshold:
                direction = "up" if momentum_pct > 0 else "down"
                strength = min(1.0, abs_momentum / (threshold * 3))

                signals.append(MomentumSignal(
                    asset=asset,
                    direction=direction,
                    momentum_pct=momentum_pct,
                    window_seconds=self.window_seconds,
                    binance_price=price_now,
                    binance_price_start=price_start,
                    timestamp_ms=self._latest_time.get(asset, int(time.time() * 1000)),
                    strength=strength,
                ))

        return signals

    def get_latest_price(self, asset: str) -> Optional[float]:
        return self._latest_price.get(asset)


# ═══════════════════════════════════════════════════════════════════════════════
# BINANCE WEBSOCKET
# ═══════════════════════════════════════════════════════════════════════════════

class BinanceWebSocket:
    """Async WebSocket connection to Binance combined trade streams.

    Connects to: wss://stream.binance.com:9443/stream?streams=...
    """

    def __init__(
        self,
        tracker: BinancePriceTracker,
        on_signal: Optional[Callable[[MomentumSignal], None]] = None,
    ):
        self.tracker = tracker
        self._on_signal = on_signal
        self._running = False
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self.stats = LatencyArbStats()

        # Build stream URL
        streams = "btcusdt@trade/ethusdt@trade/solusdt@trade"
        self.url = f"{Config.BINANCE_WS_URL}/stream?streams={streams}"

    async def start(self):
        """Start the WebSocket connection."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._connect_loop())

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _connect_loop(self):
        """Main connection loop with reconnection."""
        try:
            import websockets
        except ImportError:
            print("[BINANCE] ❌ websockets package required: pip install websockets")
            return

        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=3,
                    max_size=2**20,  # 1MB
                ) as ws:
                    self._ws = ws
                    print(f"[BINANCE] ✅ Connected to {self.url[:60]}...")

                    async for message in ws:
                        if not self._running:
                            break
                        self._handle_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self.stats.ws_reconnects += 1
                    wait = min(30, 2 ** min(self.stats.ws_reconnects, 5))
                    print(f"[BINANCE] ⚠️ Disconnected: {e}, reconnecting in {wait}s...")
                    await asyncio.sleep(wait)

    def _handle_message(self, raw: str | bytes):
        """Handle incoming message. SPEED CRITICAL PATH."""
        self.stats.binance_messages += 1

        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return

        # Combined stream format: {"stream": "btcusdt@trade", "data": {...}}
        payload = data.get("data", data)
        event_type = payload.get("e")

        if event_type != "trade":
            return

        trade = BinanceTrade(
            symbol=payload.get("s", ""),
            price=float(payload.get("p", 0)),
            quantity=float(payload.get("q", 0)),
            timestamp_ms=int(payload.get("T", 0)),
            is_buyer_maker=payload.get("m", False),
        )

        # Feed to tracker (fast path)
        self.tracker.on_trade(trade)

        # Check for momentum signals (runs every trade — must be fast)
        signals = self.tracker.check_signals()
        if signals and self._on_signal:
            for sig in signals:
                self.stats.signals_detected += 1
                self._on_signal(sig)


# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY ARB STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════

class LatencyArb:
    """Latency arbitrage strategy.

    When Binance shows a sharp move, checks if Polymarket has repriced.
    If not, fires an aggressive market buy on the correct side.

    This fires ONLY on strong signals — quality over quantity.
    """

    def __init__(
        self,
        client: Optional["PolymarketClient"] = None,
        market_cache: Optional["MarketDataCache"] = None,
        on_fire: Optional[Callable[[LatencyArbSignal], None]] = None,
    ):
        self.client = client
        self.market_cache = market_cache
        self._on_fire = on_fire

        # Config
        self.min_price_gap: float = Config.MIN_PRICE_GAP
        self.max_position: float = Config.LATENCY_MAX_POSITION

        # Binance components
        self.tracker = BinancePriceTracker()
        self.binance_ws = BinanceWebSocket(
            tracker=self.tracker,
            on_signal=self._on_momentum_signal,
        )

        # Stats
        self.stats = self.binance_ws.stats

        # Cooldown: prevent firing on same asset more than once per window
        self._last_fire: dict[str, float] = {}  # asset -> timestamp
        self._cooldown_seconds: float = 30.0

        # Signal history
        self.signal_history: list[LatencyArbSignal] = []

    async def start(self):
        """Start Binance WebSocket."""
        await self.binance_ws.start()
        print("[SNIPER] 🎯 Latency arb active — watching Binance feeds")

    async def stop(self):
        """Stop Binance WebSocket."""
        await self.binance_ws.stop()
        print("[SNIPER] Stopped")

    def _on_momentum_signal(self, signal: MomentumSignal):
        """Called when Binance shows sharp momentum. SPEED CRITICAL.

        This runs synchronously in the WebSocket message handler.
        Must complete in <5ms to keep the pipeline fast.
        """
        now = time.time()

        # Cooldown check
        last = self._last_fire.get(signal.asset, 0)
        if now - last < self._cooldown_seconds:
            self.stats.signals_skipped += 1
            return

        # Check Polymarket pricing
        arb_signal = self._check_polymarket_gap(signal)
        if arb_signal is None:
            self.stats.signals_skipped += 1
            return

        # FIRE!
        self._last_fire[signal.asset] = now
        self.stats.signals_fired += 1

        # Update rolling averages
        n = self.stats.signals_fired
        self.stats.avg_momentum_pct = (
            self.stats.avg_momentum_pct * (n - 1) + abs(signal.momentum_pct)
        ) / n
        self.stats.avg_price_gap = (
            self.stats.avg_price_gap * (n - 1) + arb_signal.price_gap
        ) / n
        self.stats.avg_signal_latency_ms = (
            self.stats.avg_signal_latency_ms * (n - 1) + arb_signal.latency_ms
        ) / n

        self.signal_history.append(arb_signal)
        if len(self.signal_history) > 500:
            self.signal_history = self.signal_history[-500:]

        print(f"[SNIPER] 🔥 FIRE {signal.asset} {signal.direction.upper()} | "
              f"Binance: {signal.momentum_pct:+.3f}% | "
              f"Poly gap: {arb_signal.price_gap:.4f} | "
              f"Latency: {arb_signal.latency_ms}ms")

        if self._on_fire:
            self._on_fire(arb_signal)

    def _check_polymarket_gap(self, signal: MomentumSignal) -> Optional[LatencyArbSignal]:
        """Check if Polymarket is lagging behind Binance.

        Returns LatencyArbSignal if there's a tradeable gap, else None.
        """
        market_type = signal.market_type_5m

        # Get current market
        now = int(time.time())
        interval = market_type.interval_seconds
        current_window = (now // interval) * interval

        market = None
        if self.client:
            try:
                market = self.client.get_market(market_type, current_window, use_cache=True)
            except Exception:
                pass

        if not market or market.closed or not market.accepting_orders:
            return None

        # The "correct" side based on Binance direction
        if signal.direction == "up":
            correct_price = market.up_price
            wrong_price = market.down_price
        else:
            correct_price = market.down_price
            wrong_price = market.up_price

        # Price gap: if Binance says UP strongly but Polymarket YES is still cheap
        # The gap is how much "value" there is
        # A good signal: correct side is trading at 35-50¢ when it should be higher
        price_gap = 0.50 - correct_price  # positive if correct side is cheap

        if price_gap < self.min_price_gap:
            return None  # Market has already repriced

        # Size: scale with signal strength, cap at max position
        base_size = min(self.max_position, Config.MAX_BET)
        size = base_size * signal.strength

        latency_ms = int(time.time() * 1000) - signal.timestamp_ms

        return LatencyArbSignal(
            momentum=signal,
            polymarket_price=correct_price,
            price_gap=price_gap,
            recommended_size=max(Config.MIN_BET, size),
            market=market,
            market_type=market_type,
            latency_ms=latency_ms,
        )

    def record_outcome(self, won: bool, pnl: float):
        """Record the outcome of a latency arb trade."""
        if won:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.total_pnl += pnl
        self.stats.total_volume += abs(pnl)

    def get_stats(self) -> dict:
        return {
            "binance_connected": self.binance_ws._running,
            "latest_prices": {
                asset: self.tracker.get_latest_price(asset)
                for asset in BinancePriceTracker.SYMBOLS.values()
            },
            **self.stats.to_dict(),
        }
