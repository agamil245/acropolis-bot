"""Telegram notification system for AcropolisBot.

Sends real-time trade alerts, settlement results, and periodic PnL updates
directly to the user via Telegram Bot API.
"""

import asyncio
import httpx
import time
from datetime import datetime, timezone
from typing import Optional

TELEGRAM_API = "https://api.telegram.org"


class TelegramNotifier:
    """Sends structured trade notifications via Telegram."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._session = None  # unused, kept for compat
        self._enabled = bool(bot_token and chat_id)
        
        # PnL update tracking
        self._last_pnl_update = 0
        self._pnl_update_interval = 300  # 5 minutes
        
        # Daily summary tracking
        self._last_daily_summary = ""
        self._daily_trades: list[dict] = []
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10)
            )
        return self._session

    async def send_message(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message via Telegram Bot API."""
        if not self._enabled:
            return False
        
        try:
            return await asyncio.wait_for(self._do_send(text, parse_mode), timeout=8)
        except asyncio.TimeoutError:
            print(f"[TELEGRAM] ❌ Send timed out (8s)")
            return False
        except Exception as e:
            print(f"[TELEGRAM] ❌ Error: {e}")
            return False

    async def _do_send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Internal send using httpx."""
        url = f"{TELEGRAM_API}/bot{self.bot_token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                return True
            else:
                print(f"[TELEGRAM] ❌ Send failed ({resp.status_code}): {resp.text}")
                return False

    # ── Trade Notifications ───────────────────────────────────────────────

    async def notify_momentum_signal(
        self, 
        asset: str, 
        direction: str, 
        change_pct: float, 
        confidence: float,
        window_ts: int,
    ):
        """Notify when a momentum signal is detected."""
        emoji = "🟢" if direction == "up" else "🔴"
        conf_bar = "🟩" * int(confidence * 10) + "⬜" * (10 - int(confidence * 10))
        
        text = (
            f"🎯 <b>MOMENTUM SIGNAL</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{emoji} <b>{asset} {direction.upper()}</b>\n"
            f"📊 Price Change: <code>{change_pct:+.4f}%</code>\n"
            f"🎲 Confidence: <code>{confidence:.0%}</code> {conf_bar}\n"
            f"⏰ Window: <code>{window_ts}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)

    async def notify_trade_opened(
        self,
        strategy: str,
        side: str,
        price: float,
        size: float,
        market_slug: str,
        extra: str = "",
    ):
        """Notify when a trade is placed."""
        if strategy == "momentum":
            emoji = "💰"
            strat_name = "MOMENTUM BET"
        elif strategy == "spread":
            emoji = "🏠"
            strat_name = "SPREAD FARM"
        else:
            emoji = "📈"
            strat_name = strategy.upper()
        
        potential_payout = size / price if price > 0 else 0
        potential_profit = potential_payout - size

        text = (
            f"{emoji} <b>{strat_name} — OPENED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Side: <b>{side}</b> @ <code>${price:.4f}</code>\n"
            f"💵 Size: <code>${size:.2f}</code>\n"
            f"🎯 Potential Payout: <code>${potential_payout:.2f}</code>\n"
            f"💰 Potential Profit: <code>${potential_profit:+.2f}</code>\n"
            f"📋 Market: <code>{market_slug}</code>\n"
        )
        if extra:
            text += f"ℹ️ {extra}\n"
        text += f"━━━━━━━━━━━━━━━━━━━"
        
        await self.send_message(text)

    async def notify_trade_closed(
        self,
        strategy: str,
        side: str,
        market_slug: str,
        outcome: str,
        pnl: float,
        won: bool,
        record_wins: int = 0,
        record_losses: int = 0,
        bankroll: float = 0,
    ):
        """Notify when a trade settles."""
        if won:
            emoji = "✅"
            result = "WON"
        else:
            emoji = "❌"
            result = "LOST"
        
        if strategy == "momentum":
            strat_name = "MOMENTUM"
        elif strategy == "spread":
            strat_name = "SPREAD"
        else:
            strat_name = strategy.upper()

        win_rate = (record_wins / (record_wins + record_losses) * 100) if (record_wins + record_losses) > 0 else 0

        text = (
            f"{emoji} <b>{strat_name} — {result}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Side: <b>{side}</b> | Outcome: <b>{outcome.upper()}</b>\n"
            f"💰 PnL: <code>${pnl:+.2f}</code>\n"
            f"📊 Record: <code>{record_wins}W/{record_losses}L ({win_rate:.1f}%)</code>\n"
            f"🏦 Bankroll: <code>${bankroll:.2f}</code>\n"
            f"📋 Market: <code>{market_slug}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)
        
        # Track for daily summary
        self._daily_trades.append({
            "strategy": strategy,
            "side": side,
            "market": market_slug,
            "outcome": outcome,
            "pnl": pnl,
            "won": won,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    async def notify_spread_posted(
        self,
        yes_price: float,
        no_price: float,
        edge_pct: float,
        size: float,
        market_slug: str,
    ):
        """Notify when a spread is posted (both legs)."""
        total = yes_price + no_price
        text = (
            f"🏠 <b>SPREAD POSTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"📍 YES@<code>{yes_price:.4f}</code> + NO@<code>{no_price:.4f}</code> = <code>${total:.4f}</code>\n"
            f"📊 Edge: <code>{edge_pct:.1f}%</code> | Size: <code>${size:.2f}</code>/leg\n"
            f"💰 Guaranteed Profit: <code>${(1.0 - total) * (size / yes_price):.2f}</code>\n"
            f"📋 <code>{market_slug}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)

    # ── Periodic Updates ──────────────────────────────────────────────────

    async def send_pnl_update(
        self,
        bankroll: float,
        total_pnl: float,
        spread_stats: dict,
        momentum_stats: dict,
        uptime_seconds: float,
    ):
        """Send periodic PnL update (every 5 minutes)."""
        now = time.time()
        if now - self._last_pnl_update < self._pnl_update_interval:
            return
        self._last_pnl_update = now

        uptime_h = int(uptime_seconds // 3600)
        uptime_m = int((uptime_seconds % 3600) // 60)

        sf_trades = spread_stats.get("trades", 0)
        sf_wins = spread_stats.get("wins", 0)
        sf_losses = spread_stats.get("losses", 0)
        sf_pnl = spread_stats.get("pnl", 0)
        sf_wr = (sf_wins / sf_trades * 100) if sf_trades > 0 else 0

        mt_trades = momentum_stats.get("trades_taken", 0)
        mt_wins = momentum_stats.get("wins", 0)
        mt_losses = momentum_stats.get("losses", 0)
        mt_pnl = momentum_stats.get("total_pnl", 0)
        mt_wr = (mt_wins / mt_trades * 100) if mt_trades > 0 else 0

        total_trades = sf_trades + mt_trades
        total_wins = sf_wins + mt_wins
        total_losses = sf_losses + mt_losses
        overall_wr = (total_wins / total_trades * 100) if total_trades > 0 else 0

        pnl_emoji = "📈" if total_pnl >= 0 else "📉"

        text = (
            f"📊 <b>5-MINUTE PnL UPDATE</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Bankroll: <code>${bankroll:.2f}</code>\n"
            f"{pnl_emoji} Total PnL: <code>${total_pnl:+.2f}</code>\n"
            f"⏱ Uptime: <code>{uptime_h}h {uptime_m}m</code>\n"
            f"\n"
            f"🏠 <b>Spread Farmer</b>\n"
            f"   Trades: <code>{sf_trades}</code> | "
            f"<code>{sf_wins}W/{sf_losses}L ({sf_wr:.1f}%)</code>\n"
            f"   PnL: <code>${sf_pnl:+.4f}</code>\n"
            f"\n"
            f"🎯 <b>Momentum</b>\n"
            f"   Trades: <code>{mt_trades}</code> | "
            f"<code>{mt_wins}W/{mt_losses}L ({mt_wr:.1f}%)</code>\n"
            f"   PnL: <code>${mt_pnl:+.2f}</code>\n"
            f"\n"
            f"📈 <b>Overall</b>\n"
            f"   Total: <code>{total_trades}T</code> | "
            f"<code>{total_wins}W/{total_losses}L ({overall_wr:.1f}%)</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)

    async def send_daily_summary(self):
        """Send end-of-day summary. Call once per day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self._last_daily_summary == today:
            return
        self._last_daily_summary = today

        if not self._daily_trades:
            return

        total_pnl = sum(t["pnl"] for t in self._daily_trades)
        wins = sum(1 for t in self._daily_trades if t["won"])
        losses = len(self._daily_trades) - wins
        wr = (wins / len(self._daily_trades) * 100) if self._daily_trades else 0

        momentum_trades = [t for t in self._daily_trades if t["strategy"] == "momentum"]
        spread_trades = [t for t in self._daily_trades if t["strategy"] == "spread"]

        mt_pnl = sum(t["pnl"] for t in momentum_trades)
        sf_pnl = sum(t["pnl"] for t in spread_trades)

        best_trade = max(self._daily_trades, key=lambda t: t["pnl"]) if self._daily_trades else None
        worst_trade = min(self._daily_trades, key=lambda t: t["pnl"]) if self._daily_trades else None

        pnl_emoji = "🟢" if total_pnl >= 0 else "🔴"

        text = (
            f"📅 <b>DAILY SUMMARY — {today}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_emoji} Total PnL: <code>${total_pnl:+.2f}</code>\n"
            f"📊 Trades: <code>{len(self._daily_trades)}</code> | "
            f"<code>{wins}W/{losses}L ({wr:.1f}%)</code>\n"
            f"\n"
            f"🏠 Spread: <code>{len(spread_trades)}T</code> | PnL: <code>${sf_pnl:+.4f}</code>\n"
            f"🎯 Momentum: <code>{len(momentum_trades)}T</code> | PnL: <code>${mt_pnl:+.2f}</code>\n"
        )

        if best_trade:
            text += (
                f"\n"
                f"🏆 Best Trade: <code>${best_trade['pnl']:+.2f}</code> "
                f"({best_trade['strategy']} {best_trade['side']} on {best_trade['market']})\n"
            )
        if worst_trade and worst_trade["pnl"] < 0:
            text += (
                f"💀 Worst Trade: <code>${worst_trade['pnl']:+.2f}</code> "
                f"({worst_trade['strategy']} {worst_trade['side']} on {worst_trade['market']})\n"
            )

        text += f"━━━━━━━━━━━━━━━━━━━"
        
        await self.send_message(text)
        
        # Reset daily trades
        self._daily_trades = []

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def notify_bot_started(self, bankroll: float, mode: str):
        """Send startup notification."""
        text = (
            f"🚀 <b>AcropolisBot STARTED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Bankroll: <code>${bankroll:.2f}</code>\n"
            f"📋 Mode: <code>{mode}</code>\n"
            f"🏠 Spread Farmer: ✅ Active\n"
            f"🎯 Momentum: ✅ Active\n"
            f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)

    async def notify_bot_stopped(self, bankroll: float, total_pnl: float, uptime: str):
        """Send shutdown notification."""
        text = (
            f"🛑 <b>AcropolisBot STOPPED</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"🏦 Final Bankroll: <code>${bankroll:.2f}</code>\n"
            f"💰 Session PnL: <code>${total_pnl:+.2f}</code>\n"
            f"⏱ Uptime: <code>{uptime}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━"
        )
        await self.send_message(text)

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
