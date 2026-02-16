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
    from src.strategies.bayesian_model import BayesianModel


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
class ChainlinkDivergenceSignal:
    """Signal from Chainlink oracle divergence vs Polymarket pricing.

    This is the PRIMARY signal source — Chainlink IS the settlement oracle.
    When Chainlink shows a clear direction but Polymarket hasn't repriced,
    that's the edge.
    """
    asset: str
    direction: str              # "up" or "down"
    chainlink_price: float
    window_start_price: float
    change_pct: float           # Chainlink % move in window
    polymarket_price: float     # current Polymarket price on correct side
    implied_fair_value: float   # what it SHOULD be trading at
    divergence: float           # implied - actual (our edge in $)
    time_left_seconds: int
    confidence: float
    market: Optional["Market"] = None
    market_type: MarketType = MarketType.BTC_5M
    timestamp_ms: int = 0

    def __post_init__(self):
        if self.timestamp_ms == 0:
            self.timestamp_ms = int(time.time() * 1000)


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

class ExchangeWebSocket:
    """Async WebSocket connection to exchange trade streams.

    Tries Binance first, falls back to Bybit if blocked (HTTP 451).
    Connects to combined trade streams for BTC/ETH/SOL.
    """

    # Exchange configs: (url, subscribe_msg_fn, parse_fn)
    EXCHANGES = [
        {
            "name": "Binance",
            "url": f"{Config.BINANCE_WS_URL}/stream?streams=btcusdt@trade/ethusdt@trade/solusdt@trade",
            "subscribe": None,  # Binance uses URL-based subscription
            "parse": "_parse_binance",
        },
        {
            "name": "Bybit",
            "url": "wss://stream.bybit.com/v5/public/spot",
            "subscribe": {
                "op": "subscribe",
                "args": ["publicTrade.BTCUSDT", "publicTrade.ETHUSDT", "publicTrade.SOLUSDT"]
            },
            "parse": "_parse_bybit",
        },
        {
            "name": "OKX",
            "url": "wss://ws.okx.com:8443/ws/v5/public",
            "subscribe": {
                "op": "subscribe",
                "args": [
                    {"channel": "trades", "instId": "BTC-USDT"},
                    {"channel": "trades", "instId": "ETH-USDT"},
                    {"channel": "trades", "instId": "SOL-USDT"},
                ]
            },
            "parse": "_parse_okx",
        },
    ]

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
        self._current_exchange: Optional[dict] = None

        # Start with Binance URL for backward compat
        self.url = self.EXCHANGES[0]["url"]

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
        """Main connection loop with reconnection and exchange fallback."""
        try:
            import websockets
        except ImportError:
            print("[EXCHANGE] ❌ websockets package required: pip install websockets")
            return

        exchange_idx = 0

        while self._running:
            exchange = self.EXCHANGES[exchange_idx % len(self.EXCHANGES)]
            self._current_exchange = exchange
            name = exchange["name"]

            try:
                async with websockets.connect(
                    exchange["url"],
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=3,
                    max_size=2**20,
                ) as ws:
                    self._ws = ws
                    print(f"[{name.upper()}] ✅ Connected to {exchange['url'][:60]}...")

                    # Send subscribe message if needed
                    if exchange.get("subscribe"):
                        await ws.send(json.dumps(exchange["subscribe"]))

                    # Reset reconnect counter on success
                    self.stats.ws_reconnects = 0

                    async for message in ws:
                        if not self._running:
                            break
                        self._handle_message(message)

            except asyncio.CancelledError:
                break
            except Exception as e:
                if self._running:
                    self.stats.ws_reconnects += 1
                    err_str = str(e)

                    # If blocked (451) or forbidden, try next exchange
                    if "451" in err_str or "403" in err_str or "refused" in err_str.lower():
                        print(f"[{name.upper()}] ❌ Blocked ({e}), trying next exchange...")
                        exchange_idx += 1
                        await asyncio.sleep(2)
                    else:
                        wait = min(30, 2 ** min(self.stats.ws_reconnects, 5))
                        print(f"[{name.upper()}] ⚠️ Disconnected: {e}, reconnecting in {wait}s...")
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

        if not self._current_exchange:
            return

        parse_method = self._current_exchange.get("parse", "_parse_binance")
        trade = getattr(self, parse_method)(data)
        if trade is None:
            return

        # Feed to tracker (fast path)
        self.tracker.on_trade(trade)

        # Feed to Bayesian model if available (via LatencyArb reference)
        if hasattr(self, '_bayesian_model') and self._bayesian_model:
            asset = self.tracker.SYMBOLS.get(trade.symbol.lower())
            if asset:
                self._bayesian_model.on_trade(
                    asset, trade.price, trade.quantity, trade.is_buyer_maker,
                )

        # Check for momentum signals (runs every trade — must be fast)
        signals = self.tracker.check_signals()
        if signals and self._on_signal:
            for sig in signals:
                self.stats.signals_detected += 1
                self._on_signal(sig)

    def _parse_binance(self, data: dict) -> Optional[BinanceTrade]:
        """Parse Binance combined stream trade message."""
        payload = data.get("data", data)
        if payload.get("e") != "trade":
            return None
        return BinanceTrade(
            symbol=payload.get("s", ""),
            price=float(payload.get("p", 0)),
            quantity=float(payload.get("q", 0)),
            timestamp_ms=int(payload.get("T", 0)),
            is_buyer_maker=payload.get("m", False),
        )

    def _parse_bybit(self, data: dict) -> Optional[BinanceTrade]:
        """Parse Bybit v5 public trade message."""
        topic = data.get("topic", "")
        if not topic.startswith("publicTrade."):
            return None
        trades = data.get("data", [])
        if not trades:
            return None
        t = trades[-1]  # Latest trade
        symbol = t.get("s", "").upper()  # "BTCUSDT"
        return BinanceTrade(
            symbol=symbol,
            price=float(t.get("p", 0)),
            quantity=float(t.get("v", 0)),
            timestamp_ms=int(t.get("T", 0)),
            is_buyer_maker=t.get("S") == "Sell",
        )

    def _parse_okx(self, data: dict) -> Optional[BinanceTrade]:
        """Parse OKX trade message."""
        if "data" not in data or "arg" not in data:
            return None
        arg = data["arg"]
        if arg.get("channel") != "trades":
            return None
        trades = data["data"]
        if not trades:
            return None
        t = trades[-1]
        # OKX instId: "BTC-USDT" -> "BTCUSDT"
        inst = t.get("instId", "").replace("-", "")
        return BinanceTrade(
            symbol=inst,
            price=float(t.get("px", 0)),
            quantity=float(t.get("sz", 0)),
            timestamp_ms=int(t.get("ts", 0)),
            is_buyer_maker=t.get("side") == "sell",
        )


# ═══════════════════════════════════════════════════════════════════════════════
# LATENCY ARB STRATEGY
# ═══════════════════════════════════════════════════════════════════════════════

class LatencyArb:
    """Latency arbitrage strategy.

    PRIMARY: Chainlink oracle divergence — reads the settlement source directly.
    SECONDARY: Binance/Bybit momentum for confirmation.

    Signal flow: Chainlink momentum → check Polymarket lag → divergence > threshold → FIRE
    The key change: instead of "Binance moved fast", detect "Chainlink says X but Polymarket says Y"
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

        # Binance components (SECONDARY confirmation)
        self.tracker = BinancePriceTracker()
        self.binance_ws = ExchangeWebSocket(
            tracker=self.tracker,
            on_signal=self._on_momentum_signal,
        )

        # Chainlink components (PRIMARY — the settlement oracle)
        self._chainlink_feed = None
        self._chainlink_momentum = None
        self._chainlink_signals: list[ChainlinkDivergenceSignal] = []

        # Stats
        self.stats = self.binance_ws.stats

        # Cooldown: prevent firing on same asset more than once per window
        self._last_fire: dict[str, float] = {}  # asset -> timestamp
        self._cooldown_seconds: float = 30.0

        # Signal history
        self.signal_history: list[LatencyArbSignal] = []

        # Bayesian model reference (set by coordinator)
        self._bayesian_model: Optional["BayesianModel"] = None

    @property
    def bayesian_model(self):
        return self._bayesian_model

    @bayesian_model.setter
    def bayesian_model(self, model):
        self._bayesian_model = model
        # Also set on the websocket so it can feed trades
        self.binance_ws._bayesian_model = model

    def set_chainlink(self, feed, momentum_detector):
        """Set Chainlink feed and momentum detector (called by bot_engine)."""
        self._chainlink_feed = feed
        self._chainlink_momentum = momentum_detector
        print("[SNIPER] 🔗 Chainlink oracle linked as PRIMARY price source")

    def check_chainlink_divergence(self) -> list[ChainlinkDivergenceSignal]:
        """Check all assets for Chainlink vs Polymarket divergence.

        This is the PRIMARY signal source. Called from bot_engine polling loop.
        Returns list of actionable divergence signals.
        """
        if not self._chainlink_momentum or not self.client:
            return []

        from src.core.chainlink_feed import get_divergence

        signals = []
        now = int(time.time())

        for asset in ["BTC", "ETH", "SOL"]:
            momentum = self._chainlink_momentum.get_momentum(asset)
            if not momentum or not momentum.is_actionable:
                continue

            # Cooldown check
            last = self._last_fire.get(f"chainlink_{asset}", 0)
            if time.time() - last < self._cooldown_seconds:
                continue

            # Get current Polymarket market
            market_type_map = {"BTC": MarketType.BTC_5M, "ETH": MarketType.ETH_5M, "SOL": MarketType.SOL_5M}
            market_type = market_type_map.get(asset)
            if not market_type:
                continue

            interval = market_type.interval_seconds
            current_window = (now // interval) * interval

            try:
                market = self.client.get_market(market_type, current_window, use_cache=True)
            except Exception:
                continue

            if not market or market.closed or not market.accepting_orders:
                continue

            # Calculate divergence
            div_signal = get_divergence(
                asset, momentum, market.up_price, market.down_price
            )

            if div_signal.is_profitable and div_signal.recommended_action != "pass":
                # Binance confirmation (SECONDARY): check if exchange agrees
                binance_price = self.tracker.get_latest_price(asset)
                binance_confirms = True
                if binance_price and momentum.window_start_price > 0:
                    binance_dir = "up" if binance_price > momentum.window_start_price else "down"
                    binance_confirms = (binance_dir == div_signal.direction)

                # Create signal
                chainlink_signal = ChainlinkDivergenceSignal(
                    asset=asset,
                    direction=div_signal.direction,
                    chainlink_price=div_signal.chainlink_price,
                    window_start_price=div_signal.window_start_price,
                    change_pct=div_signal.change_pct,
                    polymarket_price=div_signal.polymarket_price,
                    implied_fair_value=div_signal.implied_fair_value,
                    divergence=div_signal.divergence,
                    time_left_seconds=div_signal.time_left_seconds,
                    confidence=div_signal.confidence * (1.0 if binance_confirms else 0.7),
                    market=market,
                    market_type=market_type,
                )

                signals.append(chainlink_signal)
                self._chainlink_signals.append(chainlink_signal)

                # Keep history bounded
                if len(self._chainlink_signals) > 500:
                    self._chainlink_signals = self._chainlink_signals[-500:]

                print(f"[CHAINLINK] 🎯 {asset} {div_signal.direction.upper()} | "
                      f"Δ{div_signal.change_pct:+.3f}% | "
                      f"Poly: {div_signal.polymarket_price:.2f}¢ vs Fair: {div_signal.implied_fair_value:.2f}¢ | "
                      f"Edge: {div_signal.divergence:.2f}¢ | "
                      f"Time left: {div_signal.time_left_seconds}s"
                      f"{' ✓Binance' if binance_confirms else ' ✗Binance'}")

        return signals

    def consume_chainlink_signals(self) -> list[ChainlinkDivergenceSignal]:
        """Consume and clear pending Chainlink divergence signals."""
        signals = list(self._chainlink_signals)
        self._chainlink_signals.clear()
        return signals

    async def start(self):
        """Start Binance WebSocket (Chainlink is started by bot_engine)."""
        await self.binance_ws.start()
        print("[SNIPER] 🎯 Latency arb active — Chainlink PRIMARY + Binance SECONDARY")

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

        # Bayesian confirmation: only fire if posterior agrees with direction (>60% confidence)
        if self.bayesian_model:
            p_up = self.bayesian_model.get_bayesian_probability(signal.asset)
            if signal.direction == "up" and p_up < 0.60:
                self.stats.signals_skipped += 1
                return
            if signal.direction == "down" and p_up > 0.40:
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

    def record_outcome(self, won: bool, pnl: float, asset: str = "", outcome: str = ""):
        """Record the outcome of a latency arb trade and feed back to Bayesian model."""
        if won:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.total_pnl += pnl
        self.stats.total_volume += abs(pnl)

        # Feed outcome to Bayesian model for self-learning
        if self.bayesian_model and asset and outcome:
            self.bayesian_model.on_outcome(asset, outcome)

    def get_stats(self) -> dict:
        chainlink_prices = {}
        if self._chainlink_feed:
            chainlink_prices = self._chainlink_feed.get_all_prices()

        return {
            "binance_connected": self.binance_ws._running,
            "chainlink_connected": self._chainlink_feed is not None and self._chainlink_feed._initialized,
            "chainlink_prices": chainlink_prices,
            "chainlink_signals_total": len(self._chainlink_signals),
            "latest_prices": {
                asset: self.tracker.get_latest_price(asset)
                for asset in BinancePriceTracker.SYMBOLS.values()
            },
            **self.stats.to_dict(),
        }
