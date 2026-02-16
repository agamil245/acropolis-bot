"""Main bot engine coordinating all strategies."""

import asyncio
import time
from typing import Optional

from src.config import Config, MarketType
from src.core.polymarket import PolymarketClient, Market
from src.core.trader import PaperTrader, TradingState
from src.strategies.arbitrage import ArbitrageStrategy
from src.strategies.streak import evaluate as evaluate_streak, kelly_size
from src.strategies.copytrade import CopytradeMonitor


class BotEngine:
    """Main trading bot engine."""

    def __init__(self):
        self.client = PolymarketClient()
        self.state = TradingState.load()
        self.trader = PaperTrader(self.state)
        
        # Initialize strategies
        self.arb_strategy = ArbitrageStrategy() if Config.ENABLE_ARBITRAGE else None
        self.copy_monitor = CopytradeMonitor() if Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE else None
        
        # Bot status
        self.running = False
        self.last_check: dict[str, float] = {}
        
        print(f"[BOT] AcropolisBot initialized")
        print(f"[BOT] Mode: {'PAPER' if Config.PAPER_TRADE else 'LIVE'}")
        print(f"[BOT] Markets: {[m.value for m in Config.ACTIVE_MARKETS]}")
        print(f"[BOT] Strategies: ARB={Config.ENABLE_ARBITRAGE}, "
              f"STREAK={Config.ENABLE_STREAK}, COPY={Config.ENABLE_COPYTRADE}")
        print(f"[BOT] Bankroll: ${self.state.bankroll:.2f}")

    async def start(self):
        """Start the bot."""
        self.running = True
        print(f"\n[BOT] 🚀 Starting AcropolisBot...\n")
        
        # Start strategy tasks
        tasks = []
        
        if Config.ENABLE_ARBITRAGE:
            tasks.append(asyncio.create_task(self._arbitrage_loop()))
        
        if Config.ENABLE_STREAK:
            tasks.append(asyncio.create_task(self._streak_loop()))
        
        if Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE:
            tasks.append(asyncio.create_task(self._copytrade_loop()))
        
        # Settlement loop (checks for resolved markets)
        tasks.append(asyncio.create_task(self._settlement_loop()))
        
        # Wait for all tasks
        await asyncio.gather(*tasks)

    async def stop(self):
        """Stop the bot."""
        print("\n[BOT] 🛑 Stopping AcropolisBot...")
        self.running = False
        self.state.save()
        print("[BOT] State saved. Goodbye!")

    async def _arbitrage_loop(self):
        """Arbitrage strategy loop - HIGHEST PRIORITY."""
        print("[ARB] 💰 Arbitrage strategy active")
        
        while self.running:
            try:
                # Check if we can trade
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(5)
                    continue

                # Get all active markets
                markets = self.client.get_all_active_markets()
                
                # Evaluate for arbitrage opportunities
                signals = self.arb_strategy.evaluate_all_markets(markets, self.state.bankroll)
                
                # Execute top opportunities
                for signal in signals[:3]:  # Max 3 concurrent arb positions
                    # Check exposure
                    if self.arb_strategy.current_exposure >= Config.ARB_MAX_EXPOSURE:
                        break

                    # Place bet
                    trade = self.trader.place_bet(
                        market=signal.market,
                        direction=signal.direction,
                        amount=signal.recommended_size,
                        strategy="arbitrage",
                        arbitrage_edge=signal.edge_pct,
                        confidence=signal.confidence
                    )
                    
                    if trade:
                        self.arb_strategy.update_exposure(trade.amount)
                        print(f"[ARB] ⚡ Edge: {signal.edge_pct:.2f}% | "
                              f"Combined: ${signal.combined_price:.4f}")

                # Fast check interval for arbitrage
                await asyncio.sleep(Config.ARB_CHECK_INTERVAL)

            except Exception as e:
                print(f"[ARB] Error: {e}")
                await asyncio.sleep(1)

    async def _streak_loop(self):
        """Streak reversal strategy loop."""
        print("[STREAK] 📈 Streak reversal strategy active")
        
        while self.running:
            try:
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(10)
                    continue

                # Check each active market type
                for market_type in Config.ACTIVE_MARKETS:
                    # Get recent outcomes
                    outcomes = self.client.get_recent_outcomes(market_type, count=10)
                    if len(outcomes) < Config.STREAK_TRIGGER:
                        continue

                    # Evaluate streak
                    signal = evaluate_streak(outcomes, market_type)
                    
                    if not signal.should_bet:
                        continue

                    # Get next market
                    now = int(time.time())
                    interval = market_type.interval_seconds
                    next_window = ((now // interval) + 1) * interval
                    
                    # Enter ~30s before window opens
                    time_until = next_window - now
                    if time_until < Config.ENTRY_SECONDS_BEFORE or time_until > (interval - 60):
                        continue

                    market = self.client.get_market(market_type, next_window)
                    if not market or market.closed:
                        continue

                    # Calculate position size using Kelly
                    price = market.up_price if signal.direction == "up" else market.down_price
                    odds = 1.0 / price if price > 0 else 2.0
                    size = kelly_size(signal.confidence, odds, self.state.bankroll)

                    # Place bet
                    trade = self.trader.place_bet(
                        market=market,
                        direction=signal.direction,
                        amount=size,
                        strategy="streak",
                        streak_length=signal.streak_length,
                        confidence=signal.confidence
                    )
                    
                    if trade:
                        print(f"[STREAK] 📈 {signal.reason}")

                await asyncio.sleep(5)

            except Exception as e:
                print(f"[STREAK] Error: {e}")
                await asyncio.sleep(5)

    async def _copytrade_loop(self):
        """Copytrade strategy loop."""
        print("[COPY] 📋 Copytrade strategy active")
        
        while self.running:
            try:
                can_trade, reason = self.state.can_trade()
                if not can_trade:
                    await asyncio.sleep(10)
                    continue

                # Poll for new trades
                signals = self.copy_monitor.poll()
                
                for signal in signals:
                    # Get market
                    market = self.client.get_market(signal.market_type, signal.market_ts)
                    if not market or market.closed:
                        continue

                    # Use fixed copy amount or scale based on trader amount
                    copy_amount = min(Config.MIN_BET * 2, signal.usdc_amount * 0.5)
                    copy_amount = max(Config.MIN_BET, min(copy_amount, Config.MAX_BET))

                    # Apply selective filter if enabled
                    if Config.ENABLE_SELECTIVE:
                        # Check delay
                        if signal.delay_ms > Config.SELECTIVE_MAX_DELAY_MS:
                            print(f"[COPY] ⏭️ Skipped: delay {signal.delay_ms}ms too high")
                            continue

                        # Check price
                        current_price = market.up_price if signal.direction == "up" else market.down_price
                        if current_price < Config.SELECTIVE_MIN_FILL_PRICE or current_price > Config.SELECTIVE_MAX_FILL_PRICE:
                            print(f"[COPY] ⏭️ Skipped: price ${current_price:.4f} out of range")
                            continue

                    # Place bet
                    trade = self.trader.place_bet(
                        market=market,
                        direction=signal.direction,
                        amount=copy_amount,
                        strategy="copytrade",
                        copied_from=signal.wallet,
                        confidence=0.6  # Assumed confidence for copytrade
                    )
                    
                    if trade:
                        print(f"[COPY] 📋 Copied {signal.trader_name}: "
                              f"{signal.direction.upper()} ${copy_amount:.2f} "
                              f"(delay: {signal.delay_ms}ms)")

                await asyncio.sleep(Config.COPY_POLL_INTERVAL)

            except Exception as e:
                print(f"[COPY] Error: {e}")
                await asyncio.sleep(5)

    async def _settlement_loop(self):
        """Check and settle resolved markets."""
        print("[SETTLE] 🎯 Settlement monitor active")
        
        while self.running:
            try:
                # Get pending trades
                pending = [t for t in self.state.trades if t.outcome is None]
                
                for trade in pending:
                    # Check if market is resolved
                    market = self.client.get_market(trade.market_type, trade.timestamp)
                    
                    if market and market.closed and market.outcome:
                        # Settle trade
                        self.state.settle_trade(trade.id, market.outcome, market)
                        
                        emoji = "✅" if trade.won else "❌"
                        print(f"[SETTLE] {emoji} {trade.market_slug}: "
                              f"{trade.direction.upper()} -> {market.outcome.upper()} | "
                              f"P&L: ${trade.net_pnl:+.2f} | "
                              f"Bankroll: ${self.state.bankroll:.2f}")
                        
                        # Release arbitrage exposure if applicable
                        if trade.strategy == "arbitrage" and self.arb_strategy:
                            self.arb_strategy.release_exposure(trade.amount)
                        
                        self.state.save()

                await asyncio.sleep(10)  # Check every 10 seconds

            except Exception as e:
                print(f"[SETTLE] Error: {e}")
                await asyncio.sleep(10)

    def get_status(self) -> dict:
        """Get current bot status."""
        stats = self.state.get_statistics()
        
        return {
            "running": self.running,
            "mode": "PAPER" if Config.PAPER_TRADE else "LIVE",
            "active_markets": [m.value for m in Config.ACTIVE_MARKETS],
            "strategies": {
                "arbitrage": Config.ENABLE_ARBITRAGE,
                "streak": Config.ENABLE_STREAK,
                "copytrade": Config.ENABLE_COPYTRADE or Config.ENABLE_SELECTIVE,
            },
            **stats
        }
