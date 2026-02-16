"""Main bot engine coordinating all strategies with async execution,
market monitoring, state management, and event system for GUI updates.

Production-ready orchestrator that:
- Runs multiple strategies concurrently via asyncio
- Manages market monitoring across BTC/ETH/SOL × 5m/15m
- Handles trade settlement and resolution tracking
- Provides event system for real-time GUI updates
- Supports graceful start/stop with state persistence
- Pre-fetches markets and manages WebSocket connections
- Implements rate limiting and error recovery
"""

import asyncio
import signal
import time
from datetime import datetime
from enum import Enum
from typing import Optional, Callable

from src.config import Config, MarketType, LOCAL_TZ, TIMEZONE_NAME, MARKET_PROFILES
from src.core.polymarket import PolymarketClient, Market, MarketDataCache
from src.core.trader import (
    PaperTrader, LiveTrader, TradingState, Trade,
    kelly_size, fixed_bet_size,
)
from src.core.paper_trader import PaperTradingEngine


# ═══════════════════════════════════════════════════════════════════════════════
# EVENT SYSTEM
# ═══════════════════════════════════════════════════════════════════════════════

class EventType(Enum):
    """Event types emitted by the bot engine for GUI/logging."""
    BOT_STARTED = "bot_started"
    BOT_STOPPED = "bot_stopped"
    TRADE_PLACED = "trade_placed"
    TRADE_SETTLED = "trade_settled"
    STRATEGY_SIGNAL = "strategy_signal"
    MARKET_UPDATE = "market_update"
    RISK_WARNING = "risk_warning"
    CIRCUIT_BREAKER = "circuit_breaker"
    ERROR = "error"
    HEARTBEAT = "heartbeat"
    STATE_CHANGED = "state_changed"


class Event:
    """Bot event with type, data, and timestamp."""
    def __init__(self, event_type: EventType, data: dict = None):
        self.type = event_type
        self.data = data or {}
        self.timestamp = time.time()
        self.datetime = datetime.now(LOCAL_TZ)

    def __repr__(self):
        return f"Event({self.type.value}, {self.data})"


class EventBus:
    """Simple pub/sub event bus for bot engine events.

    Allows the GUI, logging, and other systems to subscribe to
    bot events without tight coupling.
    """

    def __init__(self):
        self._listeners: dict[EventType, list[Callable]] = {}
        self._global_listeners: list[Callable] = []
        self._event_history: list[Event] = []
        self._max_history = 1000

    def on(self, event_type: EventType, callback: Callable):
        """Subscribe to a specific event type."""
        if event_type not in self._listeners:
            self._listeners[event_type] = []
        self._listeners[event_type].append(callback)

    def on_all(self, callback: Callable):
        """Subscribe to all events."""
        self._global_listeners.append(callback)

    def emit(self, event: Event):
        """Emit an event to all subscribers."""
        self._event_history.append(event)
        if len(self._event_history) > self._max_history:
            self._event_history = self._event_history[-self._max_history:]

        # Notify specific listeners
        for cb in self._listeners.get(event.type, []):
            try:
                cb(event)
            except Exception as e:
                print(f"[events] Listener error: {e}")

        # Notify global listeners
        for cb in self._global_listeners:
            try:
                cb(event)
            except Exception as e:
                print(f"[events] Global listener error: {e}")

    def get_recent(self, count: int = 50, event_type: Optional[EventType] = None) -> list[Event]:
        """Get recent events, optionally filtered by type."""
        if event_type:
            filtered = [e for e in self._event_history if e.type == event_type]
            return filtered[-count:]
        return self._event_history[-count:]


# ═══════════════════════════════════════════════════════════════════════════════
# BOT ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def log(msg: str):
    """Timestamped log output."""
    ts = datetime.now(LOCAL_TZ).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")


class BotEngine:
    """Main trading bot engine coordinating all strategies.

    Architecture:
    - Each strategy runs in its own async task
    - Settlement loop checks for resolved markets
    - Heartbeat loop provides periodic status updates
    - Event bus notifies GUI of all changes
    - State is persisted after every trade and settlement
    """

    def __init__(self):
        self.client = PolymarketClient()
        self.state = TradingState.load()
        self.events = EventBus()

        # Market data cache with optional WebSocket
        self.market_cache: Optional[MarketDataCache] = None
        if Config.USE_WEBSOCKET:
            try:
                self.market_cache = MarketDataCache(use_websocket=True)
            except Exception as e:
                log(f"⚠️ WebSocket init failed: {e}, using REST only")

        # Initialize trader
        if Config.PAPER_TRADE:
            self.trader = PaperTrader(self.state, self.market_cache)
        else:
            self.trader = LiveTrader(self.state, self.market_cache)

        # Independent paper trading engine (separate from main bot mode)
        self.paper_engine = PaperTradingEngine(market_cache=self.market_cache)
        self.paper_running = False
        self._paper_task: Optional[asyncio.Task] = None

        # Chainlink oracle feed
        self._chainlink_feed = None
        self._chainlink_momentum = None

        # Import strategies lazily to avoid circular imports
        self._arb_strategy = None
        self._copy_monitor = None

        # Bot status
        self.running = False
        self._tasks: list[asyncio.Task] = []
        self._start_time: Optional[float] = None

        # Track bets placed per market to avoid duplicates
        self._bet_timestamps: dict[str, set[int]] = {}  # strategy -> set of timestamps
        for s in ["arbitrage", "streak", "copytrade"]:
            self._bet_timestamps[s] = set()

        # Session stats
        self.session_wins = 0
        self.session_losses = 0
        self.session_pnl = 0.0

        # Print startup info
        Config.print_summary()
        log(f"Loaded state: bankroll=${self.state.bankroll:.2f}, "
            f"{len(self.state.trades)} trades, "
            f"{len(self.state.get_pending_trades())} pending")

    def _init_chainlink(self):
        """Initialize Chainlink oracle price feed — THE edge."""
        try:
            from src.core.chainlink_feed import ChainlinkPriceFeed, ChainlinkMomentumDetector
            self._chainlink_feed = ChainlinkPriceFeed()
            self._chainlink_momentum = ChainlinkMomentumDetector(self._chainlink_feed)
            log("🔗 Chainlink oracle feed initialized (BTC/ETH/SOL on Polygon)")
        except ImportError as e:
            log(f"⚠️ Chainlink feed unavailable (install web3): {e}")
        except Exception as e:
            log(f"⚠️ Chainlink feed init error: {e}")

    def _init_strategies(self):
        """Initialize strategy modules."""
        # Initialize Chainlink first — it's the primary data source
        self._init_chainlink()

        if Config.ENABLE_ARBITRAGE:
            try:
                from src.strategies.arbitrage import ArbitrageStrategy
                self._arb_strategy = ArbitrageStrategy(
                    client=self.client,
                    market_cache=self.market_cache,
                )
                log("⚡ Hybrid strategy loaded (Spread Farmer + Latency Arb)")
                # Link Chainlink as PRIMARY price source
                if self._chainlink_feed and self._chainlink_momentum:
                    self._arb_strategy.latency_arb.set_chainlink(
                        self._chainlink_feed, self._chainlink_momentum
                    )
            except ImportError as e:
                log(f"⚠️ Hybrid strategy unavailable: {e}")

        if Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE:
            try:
                from src.strategies.copytrade import CopytradeMonitor
                self._copy_monitor = CopytradeMonitor()
                log("📋 Copytrade monitor loaded")
            except ImportError as e:
                log(f"⚠️ Copytrade monitor unavailable: {e}")

    # ─── Start / Stop ─────────────────────────────────────────────────────

    async def start(self):
        """Start the bot engine with all strategy loops."""
        self.running = True
        self._start_time = time.time()

        log("🚀 Starting AcropolisBot...")

        # Start WebSocket
        if self.market_cache:
            self.market_cache.start()
            await asyncio.sleep(1)  # Wait for connection

        # Pre-fetch upcoming markets
        log("Pre-fetching upcoming markets...")
        for market_type in Config.ACTIVE_MARKETS:
            upcoming = self.client.get_upcoming_market_timestamps(market_type, count=3)
            self.client.prefetch_markets(upcoming, market_type)

        # Initialize strategies
        self._init_strategies()

        # Emit start event
        self.events.emit(Event(EventType.BOT_STARTED, {
            "mode": "PAPER" if Config.PAPER_TRADE else "LIVE",
            "markets": [m.value for m in Config.ACTIVE_MARKETS],
            "bankroll": self.state.bankroll,
        }))

        # Start Chainlink oracle feed
        if self._chainlink_feed:
            try:
                await self._chainlink_feed.start()
            except Exception as e:
                log(f"⚠️ Failed to start Chainlink feed: {e}")

        # Start hybrid strategy layers (Binance WS + spread farming)
        if Config.ENABLE_ARBITRAGE and self._arb_strategy:
            try:
                await self._arb_strategy.start_layers()
            except Exception as e:
                log(f"⚠️ Failed to start hybrid layers: {e}")

        # Launch strategy tasks
        self._tasks = []

        if Config.ENABLE_ARBITRAGE and self._arb_strategy:
            self._tasks.append(asyncio.create_task(self._arbitrage_loop()))
            self._tasks.append(asyncio.create_task(self._latency_arb_loop()))
            if self._chainlink_feed:
                self._tasks.append(asyncio.create_task(self._chainlink_divergence_loop()))

        if Config.ENABLE_STREAK:
            self._tasks.append(asyncio.create_task(self._streak_loop()))

        if (Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE) and self._copy_monitor:
            self._tasks.append(asyncio.create_task(self._copytrade_loop()))

        # Always run settlement and heartbeat
        self._tasks.append(asyncio.create_task(self._settlement_loop()))
        self._tasks.append(asyncio.create_task(self._heartbeat_loop()))

        log(f"🏛️ AcropolisBot running with {len(self._tasks)} tasks\n")

        # Wait for all tasks
        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """Stop the bot gracefully."""
        log("\n🛑 Stopping AcropolisBot...")
        self.running = False

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()

        # Wait for cancellation
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

        # Stop Chainlink feed
        if self._chainlink_feed:
            try:
                await self._chainlink_feed.stop()
            except Exception:
                pass

        # Stop hybrid strategy layers
        if self._arb_strategy:
            try:
                await self._arb_strategy.stop_layers()
            except Exception as e:
                log(f"⚠️ Error stopping hybrid layers: {e}")

        # Stop WebSocket
        if self.market_cache:
            self.market_cache.stop()

        # Mark pending trades
        self.state.mark_pending_as_force_exit("shutdown")

        # Save final state
        self.state.save()

        # Print summary
        uptime = time.time() - self._start_time if self._start_time else 0
        hours = int(uptime // 3600)
        mins = int((uptime % 3600) // 60)

        stats = self.state.get_statistics()
        log(f"📊 Session: {self.session_wins}W/{self.session_losses}L | "
            f"PnL: ${self.session_pnl:+.2f} | Uptime: {hours}h {mins}m")
        log(f"💰 Final bankroll: ${self.state.bankroll:.2f}")

        self.events.emit(Event(EventType.BOT_STOPPED, {
            "uptime_seconds": uptime,
            "session_pnl": self.session_pnl,
            "final_bankroll": self.state.bankroll,
        }))

        log("Goodbye! 🏛️")

    # ─── Arbitrage Strategy Loop ──────────────────────────────────────────

    async def _arbitrage_loop(self):
        """Hybrid strategy: spread farming + legacy arb scanning."""
        log("[HYBRID] ⚡ Hybrid strategy active (Layer 1: Spread Farmer)")

        while self.running:
            try:
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(5)
                    continue

                # Get all active markets
                markets = self.client.get_all_active_markets()

                # Tick spread farmer (Layer 1)
                if self._arb_strategy:
                    await self._arb_strategy.tick_spread_farmer(markets)

                # Legacy arb scan for mispriced markets
                signals = self._arb_strategy.evaluate_all_markets(markets, self.state.bankroll)

                for signal in signals[:3]:
                    if self._arb_strategy.current_exposure >= Config.ARB_MAX_EXPOSURE:
                        break

                    ts_key = signal.market.timestamp
                    if ts_key in self._bet_timestamps["arbitrage"]:
                        continue

                    if Config.USE_KELLY:
                        amount = kelly_size(
                            signal.confidence,
                            1.0 / signal.market.get_price(signal.direction) if signal.market.get_price(signal.direction) > 0 else 2.0,
                            self.state.bankroll,
                        )
                    else:
                        amount = fixed_bet_size(self.state.bankroll)

                    amount = min(amount, Config.ARB_MAX_EXPOSURE - self._arb_strategy.current_exposure)
                    amount = max(Config.ARB_MIN_BET, amount)

                    trade = self.trader.place_bet(
                        market=signal.market,
                        direction=signal.direction,
                        amount=amount,
                        strategy="arbitrage",
                        arbitrage_edge=signal.edge_pct,
                        confidence=signal.confidence,
                    )

                    if trade:
                        self._bet_timestamps["arbitrage"].add(ts_key)
                        self._arb_strategy.update_exposure(trade.amount)
                        self.events.emit(Event(EventType.TRADE_PLACED, {
                            "trade_id": trade.id,
                            "strategy": "arbitrage",
                            "direction": trade.direction,
                            "amount": trade.amount,
                            "edge_pct": signal.edge_pct,
                            "source": signal.source,
                        }))

                await asyncio.sleep(Config.ARB_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[HYBRID] Error: {e}")
                self.events.emit(Event(EventType.ERROR, {"strategy": "arbitrage", "error": str(e)}))
                await asyncio.sleep(1)

    async def _latency_arb_loop(self):
        """Layer 2: Consume latency arb signals from Binance feed."""
        log("[SNIPER] 🎯 Latency arb loop active (Layer 2)")

        while self.running:
            try:
                if not self._arb_strategy:
                    await asyncio.sleep(5)
                    continue

                signals = self._arb_strategy.consume_latency_signals()

                for signal in signals:
                    can_trade, reason = self.state.can_trade()
                    if not can_trade:
                        continue

                    market = signal.market
                    if not market or market.closed:
                        continue

                    direction = signal.momentum.direction
                    amount = min(signal.recommended_size, self.state.bankroll * 0.15)
                    amount = max(Config.MIN_BET, amount)

                    trade = self.trader.place_bet(
                        market=market,
                        direction=direction,
                        amount=amount,
                        strategy="arbitrage",
                        arbitrage_edge=signal.price_gap * 100,
                        confidence=min(0.95, 0.5 + signal.momentum.strength * 0.4),
                    )

                    if trade:
                        signal.order_sent_at_ms = int(time.time() * 1000)
                        self._arb_strategy.update_exposure(trade.amount)
                        self._arb_strategy.stats.latency_pnl += 0  # updated on settlement
                        log(f"[SNIPER] 🔥 Fired: {direction.upper()} ${amount:.2f} on {market.slug} "
                            f"| Binance: {signal.momentum.momentum_pct:+.3f}% "
                            f"| Gap: {signal.price_gap:.4f}")
                        self.events.emit(Event(EventType.TRADE_PLACED, {
                            "trade_id": trade.id,
                            "strategy": "latency_arb",
                            "direction": direction,
                            "amount": trade.amount,
                            "momentum_pct": signal.momentum.momentum_pct,
                            "price_gap": signal.price_gap,
                            "latency_ms": signal.latency_ms,
                        }))

                await asyncio.sleep(0.05)  # 50ms — fast polling for signals

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[SNIPER] Error: {e}")
                await asyncio.sleep(1)

    # ─── Chainlink Divergence Loop ────────────────────────────────────────

    async def _chainlink_divergence_loop(self):
        """Layer 2+: Poll Chainlink oracle for divergence vs Polymarket.

        This is THE edge. Chainlink IS the settlement oracle.
        We read the answer sheet before Polymarket grades the test.
        """
        log("[CHAINLINK] 🔗 Oracle divergence loop active (PRIMARY signal source)")

        while self.running:
            try:
                if not self._arb_strategy:
                    await asyncio.sleep(5)
                    continue

                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(2)
                    continue

                # Check for Chainlink divergence signals
                signals = self._arb_strategy.latency_arb.check_chainlink_divergence()

                for signal in signals:
                    market = signal.market
                    if not market or market.closed:
                        continue

                    direction = signal.direction
                    # Size based on divergence magnitude and confidence
                    base = min(self.max_position_usd(), Config.LATENCY_MAX_POSITION)
                    amount = base * signal.confidence
                    amount = max(Config.MIN_BET, min(amount, Config.MAX_BET))

                    trade = self.trader.place_bet(
                        market=market,
                        direction=direction,
                        amount=amount,
                        strategy="arbitrage",
                        arbitrage_edge=signal.divergence * 100,
                        confidence=signal.confidence,
                    )

                    if trade:
                        # Mark cooldown
                        self._arb_strategy.latency_arb._last_fire[f"chainlink_{signal.asset}"] = time.time()
                        if self._arb_strategy:
                            self._arb_strategy.update_exposure(trade.amount)

                        log(f"[CHAINLINK] 🔥 FIRE {signal.asset} {direction.upper()} ${amount:.2f} | "
                            f"Oracle Δ{signal.change_pct:+.3f}% | "
                            f"Edge: {signal.divergence:.2f}¢ | "
                            f"Time left: {signal.time_left_seconds}s")

                        self.events.emit(Event(EventType.TRADE_PLACED, {
                            "trade_id": trade.id,
                            "strategy": "chainlink_oracle",
                            "direction": direction,
                            "amount": trade.amount,
                            "chainlink_change_pct": signal.change_pct,
                            "divergence": signal.divergence,
                            "time_left": signal.time_left_seconds,
                        }))

                await asyncio.sleep(Config.CHAINLINK_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[CHAINLINK] Error: {e}")
                await asyncio.sleep(2)

    def max_position_usd(self) -> float:
        """Max position size based on bankroll."""
        return self.state.bankroll * Config.MAX_POSITION_SIZE_PCT

    # ─── Streak Reversal Strategy Loop ────────────────────────────────────

    async def _streak_loop(self):
        """Streak reversal: bet against streaks of consecutive same outcomes."""
        log("[STREAK] 📈 Streak reversal strategy active")

        while self.running:
            try:
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(10)
                    continue

                for market_type in Config.ACTIVE_MARKETS:
                    try:
                        await self._check_streak_for_market(market_type)
                    except Exception as e:
                        log(f"[STREAK] Error on {market_type.display_name}: {e}")

                await asyncio.sleep(5)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[STREAK] Error: {e}")
                await asyncio.sleep(5)

    async def _check_streak_for_market(self, market_type: MarketType):
        """Check for streak signal on a specific market type."""
        # Get recent outcomes
        outcomes = self.client.get_recent_outcomes(market_type, count=Config.STREAK_TRIGGER + 2)
        if len(outcomes) < Config.STREAK_TRIGGER:
            return

        # Evaluate streak
        try:
            from src.strategies.streak import evaluate as evaluate_streak
            signal = evaluate_streak(outcomes, market_type)
        except ImportError:
            return

        if not signal.should_bet:
            return

        # Calculate next market window
        now = int(time.time())
        interval = market_type.interval_seconds
        next_window = ((now // interval) + 1) * interval

        # Timing check
        seconds_until = next_window - now
        if seconds_until > Config.ENTRY_SECONDS_BEFORE or seconds_until < 5:
            return

        # Deduplicate
        ts_key = next_window
        if ts_key in self._bet_timestamps["streak"]:
            return

        # Get market
        market = self.client.get_market(market_type, next_window)
        if not market or market.closed or not market.accepting_orders:
            return

        # Calculate bet size
        price = market.get_price(signal.direction)
        odds = 1.0 / price if price > 0 else 2.0

        if Config.USE_KELLY:
            amount = kelly_size(signal.confidence, odds, self.state.bankroll)
        else:
            amount = fixed_bet_size(self.state.bankroll)

        # Place bet
        trade = self.trader.place_bet(
            market=market,
            direction=signal.direction,
            amount=amount,
            strategy="streak",
            streak_length=signal.streak_length,
            confidence=signal.confidence,
        )

        if trade:
            self._bet_timestamps["streak"].add(ts_key)
            log(f"[STREAK] {signal.reason}")
            self.events.emit(Event(EventType.TRADE_PLACED, {
                "trade_id": trade.id,
                "strategy": "streak",
                "market_type": market_type.value,
                "direction": trade.direction,
                "amount": trade.amount,
                "streak_length": signal.streak_length,
            }))

    # ─── Copytrade Strategy Loop ──────────────────────────────────────────

    async def _copytrade_loop(self):
        """Copytrade: monitor wallets and copy their trades."""
        log("[COPY] 📋 Copytrade strategy active")

        if not Config.COPY_WALLETS:
            log("[COPY] ⚠️ No wallets configured, copytrade inactive")
            return

        log(f"[COPY] Tracking {len(Config.COPY_WALLETS)} wallet(s)")
        for w in Config.COPY_WALLETS:
            log(f"[COPY]   └─ {w[:10]}...{w[-6:]}")

        copied_markets: set[tuple[str, int]] = set()

        while self.running:
            try:
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(10)
                    continue

                # Poll for new signals
                signals = self._copy_monitor.poll()

                for sig in signals:
                    key = (sig.wallet, sig.market_ts)
                    if key in copied_markets:
                        continue

                    # Skip sells if configured
                    if Config.COPY_ONLY_BUYS and sig.side != "BUY":
                        copied_markets.add(key)
                        continue

                    # Get market
                    market = self.client.get_market(sig.market_type, sig.market_ts)
                    if not market or market.closed or not market.accepting_orders:
                        copied_markets.add(key)
                        continue

                    # Selective filter
                    if Config.ENABLE_SELECTIVE:
                        current_price = market.get_price(sig.direction)
                        if current_price < Config.SELECTIVE_MIN_FILL_PRICE:
                            copied_markets.add(key)
                            continue
                        if current_price > Config.SELECTIVE_MAX_FILL_PRICE:
                            copied_markets.add(key)
                            continue
                        if hasattr(sig, 'delay_ms') and sig.delay_ms > Config.SELECTIVE_MAX_DELAY_MS:
                            copied_markets.add(key)
                            continue

                    # Bet size
                    copy_amount = min(Config.BET_AMOUNT, self.state.bankroll)
                    copy_amount = max(Config.MIN_BET, min(copy_amount, Config.MAX_BET))

                    # Copy delay
                    now_ms = int(time.time() * 1000)
                    copy_delay_ms = now_ms - (sig.trade_ts * 1000)

                    trade = self.trader.place_bet(
                        market=market,
                        direction=sig.direction.lower(),
                        amount=copy_amount,
                        strategy="copytrade",
                        copied_from=sig.wallet,
                        trader_name=sig.trader_name,
                        trader_direction=sig.direction,
                        trader_amount=sig.usdc_amount,
                        trader_price=sig.price,
                        trader_timestamp=sig.trade_ts,
                        copy_delay_ms=copy_delay_ms,
                        confidence=0.6,
                    )

                    if trade:
                        copied_markets.add(key)
                        log(f"[COPY] 📋 Copied {sig.trader_name}: {sig.direction.upper()} "
                            f"${copy_amount:.2f} (delay: {copy_delay_ms}ms)")
                        self.events.emit(Event(EventType.TRADE_PLACED, {
                            "trade_id": trade.id,
                            "strategy": "copytrade",
                            "copied_from": sig.trader_name,
                            "direction": trade.direction,
                            "amount": trade.amount,
                            "delay_ms": copy_delay_ms,
                        }))

                await asyncio.sleep(Config.COPY_POLL_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[COPY] Error: {e}")
                await asyncio.sleep(5)

    # ─── Settlement Loop ──────────────────────────────────────────────────

    async def _settlement_loop(self):
        """Check and settle resolved markets."""
        log("[SETTLE] 🎯 Settlement monitor active")

        while self.running:
            try:
                pending = self.state.get_pending_trades()

                for trade in pending:
                    try:
                        # Fresh fetch (no cache) for resolution status
                        market = self.client.get_market(
                            trade.market_type, trade.timestamp, use_cache=False
                        )

                        if market and market.closed and market.outcome:
                            self.state.settle_trade(trade, market.outcome, market)

                            # Update session stats
                            if trade.won:
                                self.session_wins += 1
                            else:
                                self.session_losses += 1
                            self.session_pnl += trade.net_pnl

                            emoji = "✅" if trade.won else "❌"
                            win_rate = (self.session_wins / (self.session_wins + self.session_losses) * 100
                                        if (self.session_wins + self.session_losses) > 0 else 0)

                            log(f"[SETTLE] {emoji} {trade.market_slug}: "
                                f"{trade.direction.upper()} -> {market.outcome.upper()} | "
                                f"PnL: ${trade.net_pnl:+.2f} | "
                                f"Bank: ${self.state.bankroll:.2f} | "
                                f"{self.session_wins}W/{self.session_losses}L ({win_rate:.0f}%)")

                            # Release arb exposure
                            if trade.strategy == "arbitrage" and self._arb_strategy:
                                self._arb_strategy.release_exposure(trade.amount)

                            # Feed outcome to Bayesian model for self-learning
                            if self._arb_strategy and hasattr(self._arb_strategy, 'bayesian_model'):
                                asset = trade.market_type.asset if hasattr(trade, 'market_type') else "BTC"
                                self._arb_strategy.bayesian_model.on_outcome(asset, market.outcome)

                            self.state.save()

                            self.events.emit(Event(EventType.TRADE_SETTLED, {
                                "trade_id": trade.id,
                                "strategy": trade.strategy,
                                "direction": trade.direction,
                                "outcome": market.outcome,
                                "won": trade.won,
                                "pnl": trade.net_pnl,
                                "bankroll": self.state.bankroll,
                            }))

                            # Check for circuit breaker activation
                            if self.state.circuit_breaker_active:
                                self.events.emit(Event(EventType.CIRCUIT_BREAKER, {
                                    "consecutive_losses": self.state.consecutive_losses,
                                    "cooldown_minutes": Config.COOLDOWN_MINUTES,
                                }))

                    except Exception as e:
                        log(f"[SETTLE] Error settling {trade.id}: {e}")

                await asyncio.sleep(Config.SETTLEMENT_CHECK_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[SETTLE] Error: {e}")
                await asyncio.sleep(10)

    # ─── Heartbeat Loop ───────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        """Periodic status updates and maintenance."""
        while self.running:
            try:
                await asyncio.sleep(60)  # Every minute

                stats = self.state.get_statistics()
                pending = stats["pending_trades"]
                win_rate = stats["win_rate"]

                ws_status = "WS:✓" if (self.market_cache and self.market_cache.ws_connected) else "WS:✗"

                log(f"[♥] Pending: {pending} | "
                    f"Session: {self.session_wins}W/{self.session_losses}L ({win_rate:.0f}%) | "
                    f"PnL: ${self.session_pnl:+.2f} | "
                    f"Bank: ${self.state.bankroll:.2f} | {ws_status}")

                self.events.emit(Event(EventType.HEARTBEAT, {
                    "pending": pending,
                    "bankroll": self.state.bankroll,
                    "session_pnl": self.session_pnl,
                    "ws_connected": self.market_cache.ws_connected if self.market_cache else False,
                }))

                # Periodic market pre-fetch
                for market_type in Config.ACTIVE_MARKETS:
                    try:
                        upcoming = self.client.get_upcoming_market_timestamps(market_type, count=3)
                        self.client.prefetch_markets(upcoming, market_type)
                        if self.market_cache:
                            self.market_cache.prefetch_markets(upcoming, market_type)
                    except Exception:
                        pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[♥] Heartbeat error: {e}")
                await asyncio.sleep(30)

    # ─── Paper Trading (Independent) ─────────────────────────────────────

    async def start_paper(self):
        """Start independent paper trading settlement loop."""
        if self.paper_running:
            return
        self.paper_running = True
        self.paper_engine.state.running = True
        self._paper_task = asyncio.create_task(self._paper_settlement_loop())
        log("📝 Paper trading mode STARTED")

    async def stop_paper(self):
        """Stop independent paper trading."""
        self.paper_running = False
        self.paper_engine.state.running = False
        if self._paper_task:
            self._paper_task.cancel()
            try:
                await self._paper_task
            except asyncio.CancelledError:
                pass
            self._paper_task = None
        log("📝 Paper trading mode STOPPED")

    async def _paper_settlement_loop(self):
        """Check paper trade settlements periodically."""
        while self.paper_running:
            try:
                self.paper_engine.check_settlements()
                await asyncio.sleep(Config.SETTLEMENT_CHECK_INTERVAL)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log(f"[PAPER-SIM] Settlement loop error: {e}")
                await asyncio.sleep(10)

    # ─── Status & Control ─────────────────────────────────────────────────

    def get_status(self) -> dict:
        """Get comprehensive bot status for GUI."""
        stats = self.state.get_statistics()

        uptime = time.time() - self._start_time if self._start_time else 0

        return {
            "running": self.running,
            "mode": "PAPER" if Config.PAPER_TRADE else "LIVE",
            "paper_trading": {
                "running": self.paper_running,
                "stats": self.paper_engine.state.get_stats(),
            },
            "uptime_seconds": uptime,
            "active_markets": [m.value for m in Config.ACTIVE_MARKETS],
            "strategies": {
                "arbitrage": Config.ENABLE_ARBITRAGE and self._arb_strategy is not None,
                "streak": Config.ENABLE_STREAK,
                "copytrade": (Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE)
                             and self._copy_monitor is not None,
            },
            "websocket_connected": self.market_cache.ws_connected if self.market_cache else False,
            "session": {
                "wins": self.session_wins,
                "losses": self.session_losses,
                "pnl": self.session_pnl,
            },
            **stats,
        }

    def get_recent_trades(self, count: int = 20) -> list[dict]:
        """Get recent trades for GUI display."""
        trades = self.state.trades[-count:]
        return [t.to_nested_json() for t in trades]

    def get_pending_trades(self) -> list[dict]:
        """Get pending (unsettled) trades for GUI display."""
        pending = self.state.get_pending_trades()
        return [t.to_nested_json() for t in pending]


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

async def run_bot():
    """Main entry point for running the bot."""
    engine = BotEngine()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        asyncio.ensure_future(engine.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await engine.start()
    except KeyboardInterrupt:
        await engine.stop()
    except Exception as e:
        log(f"Fatal error: {e}")
        await engine.stop()
        raise
