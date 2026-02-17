"""Directional momentum strategy for 5-min crypto markets.

Uses Binance.US price feed to detect early momentum in 5-min windows.
If BTC moves significantly in the first 1-2 minutes, bet on that direction
continuing through settlement.

This is the "2x payoff" strategy — buy one side at ~$0.50, win $1.00 if right.
Risk: lose entire bet if wrong. Need >50% win rate to be profitable.

Edge sources:
1. Binance.US real-time price vs Polymarket mid-price lag
2. 1-min candle momentum in first half of 5-min window
3. Volume-weighted price trend detection
"""

import asyncio
import time
import aiohttp
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING
from datetime import datetime

if TYPE_CHECKING:
    from src.core.polymarket import Market

BINANCE_US_API = "https://api.binance.us/api/v3"
BYBIT_API = "https://api.bybit.com/v5/market"


@dataclass
class MomentumSignal:
    """A directional signal with confidence."""
    direction: str          # "up" or "down"
    confidence: float       # 0.0 to 1.0
    price_change_pct: float # % change detected
    source: str             # "binance_us" or "bybit"
    timestamp: float = field(default_factory=time.time)


@dataclass 
class MomentumStats:
    total_signals: int = 0
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    biggest_win: float = 0.0
    biggest_loss: float = 0.0
    
    @property
    def win_rate(self) -> float:
        if self.trades_taken == 0:
            return 0.0
        return (self.wins / self.trades_taken) * 100
    
    def to_dict(self) -> dict:
        return {
            "total_signals": self.total_signals,
            "trades_taken": self.trades_taken,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": f"{self.win_rate:.1f}%",
            "total_pnl": f"${self.total_pnl:+.2f}",
            "biggest_win": f"${self.biggest_win:+.2f}",
            "biggest_loss": f"${self.biggest_loss:+.2f}",
        }


class MomentumStrategy:
    """Directional betting based on early-window price momentum.
    
    Logic:
    - At the start of each 5-min window, record BTC price from Binance.US
    - After 60-90 seconds, check the price change
    - If price moved > threshold in one direction, bet that direction
    - Buy at ~$0.50, win $1.00 if right (2x payoff)
    
    Parameters:
    - min_move_pct: Minimum % move to trigger a signal (default 0.05%)
    - strong_move_pct: Strong signal threshold (default 0.15%)
    - entry_window: Seconds into the 5-min window to check (default 60-120s)
    - bet_size: Fixed bet amount per trade
    """
    
    def __init__(self, bet_size: float = 10.0):
        self.bet_size = bet_size
        self.min_move_pct = 0.05       # 0.05% minimum move to trigger
        self.strong_move_pct = 0.15    # 0.15% = strong signal, bet bigger
        self.entry_window_start = 60   # Start checking at 60s into window
        self.entry_window_end = 150    # Stop entering after 2.5 min
        
        # Price tracking
        self._window_open_price: Optional[float] = None
        self._window_start_time: Optional[float] = None
        self._current_window_ts: Optional[int] = None
        self._already_bet_this_window: bool = False
        
        # State
        self.active_bets: list[dict] = []
        self.completed_bets: list[dict] = []
        self.stats = MomentumStats()
        
        # Trading state reference (set by bot engine)
        self._trading_state = None
        
        # Session for HTTP requests
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            )
        return self._session
    
    async def get_btc_price(self) -> Optional[float]:
        """Get current BTC price from Binance.US (fallback to Bybit)."""
        session = await self._get_session()
        
        # Try Binance.US first
        try:
            async with session.get(
                f"{BINANCE_US_API}/ticker/price",
                params={"symbol": "BTCUSDT"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data.get("price", 0))
                    if price > 0:
                        return price
        except Exception:
            pass
        
        # Fallback to Bybit
        try:
            async with session.get(
                f"{BYBIT_API}/tickers",
                params={"category": "spot", "symbol": "BTCUSDT"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    items = data.get("result", {}).get("list", [])
                    if items:
                        return float(items[0]["lastPrice"])
        except Exception:
            pass
        
        return None
    
    async def get_eth_price(self) -> Optional[float]:
        """Get current ETH price from Binance.US."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{BINANCE_US_API}/ticker/price",
                params={"symbol": "ETHUSDT"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
        except Exception:
            pass
        return None
    
    async def get_sol_price(self) -> Optional[float]:
        """Get current SOL price from Binance.US."""
        session = await self._get_session()
        try:
            async with session.get(
                f"{BINANCE_US_API}/ticker/price",
                params={"symbol": "SOLUSDT"}
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data.get("price", 0))
        except Exception:
            pass
        return None
    
    def _get_window_ts(self) -> int:
        """Get the current 5-min window timestamp."""
        now = int(time.time())
        return now - (now % 300)
    
    def _seconds_into_window(self) -> float:
        """How many seconds into the current 5-min window."""
        now = time.time()
        window_start = now - (now % 300)
        return now - window_start
    
    async def check_momentum(self, asset: str = "BTC") -> Optional[MomentumSignal]:
        """Check if there's momentum worth betting on.
        
        Called every ~5 seconds by the bot engine.
        """
        window_ts = self._get_window_ts()
        secs_in = self._seconds_into_window()
        
        # New window — record opening price
        if window_ts != self._current_window_ts:
            self._current_window_ts = window_ts
            self._already_bet_this_window = False
            
            if asset == "BTC":
                price = await self.get_btc_price()
            elif asset == "ETH":
                price = await self.get_eth_price()
            else:
                price = await self.get_sol_price()
            
            if price:
                self._window_open_price = price
                self._window_start_time = time.time()
                print(f"[MOMENTUM] 📌 Window {window_ts} opened: {asset} ${price:,.2f}")
            return None
        
        # Already bet this window
        if self._already_bet_this_window:
            return None
        
        # Only check during entry window (60-150 seconds in)
        if secs_in < self.entry_window_start or secs_in > self.entry_window_end:
            return None
        
        # Need opening price
        if self._window_open_price is None:
            return None
        
        # Get current price
        if asset == "BTC":
            current_price = await self.get_btc_price()
        elif asset == "ETH":
            current_price = await self.get_eth_price()
        else:
            current_price = await self.get_sol_price()
        
        if current_price is None:
            return None
        
        # Calculate momentum
        change_pct = ((current_price - self._window_open_price) / self._window_open_price) * 100
        abs_change = abs(change_pct)
        
        # Check if move is significant enough
        if abs_change < self.min_move_pct:
            return None
        
        # We have a signal!
        direction = "up" if change_pct > 0 else "down"
        
        # Confidence based on magnitude
        if abs_change >= self.strong_move_pct:
            confidence = 0.80
        elif abs_change >= self.min_move_pct * 2:
            confidence = 0.65
        else:
            confidence = 0.55
        
        self.stats.total_signals += 1
        
        signal = MomentumSignal(
            direction=direction,
            confidence=confidence,
            price_change_pct=change_pct,
            source="binance_us",
        )
        
        print(f"[MOMENTUM] 🎯 Signal: {asset} {direction.upper()} "
              f"(change: {change_pct:+.4f}%, confidence: {confidence:.0%}) "
              f"at {secs_in:.0f}s into window {window_ts}")
        
        with open("/tmp/spread_trades.log", "a") as _f:
            from datetime import datetime
            _f.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:12]} "
                     f"[MOMENTUM] 🎯 {asset} {direction.upper()} "
                     f"| change: {change_pct:+.4f}% | conf: {confidence:.0%} "
                     f"| window: {window_ts}\n")
            _f.flush()
        
        return signal
    
    async def place_directional_bet(
        self, 
        market: "Market",
        signal: MomentumSignal,
        paper: bool = True,
    ) -> Optional[dict]:
        """Place a directional bet based on momentum signal."""
        if self._already_bet_this_window:
            return None
        
        self._already_bet_this_window = True
        
        # Determine which side to buy
        if signal.direction == "up":
            buy_side = "YES"
            buy_price = market.up_price if market.up_price < 0.60 else 0.50
        else:
            buy_side = "NO"
            buy_price = market.down_price if market.down_price < 0.60 else 0.50
        
        # Dynamic bet size based on confidence
        if signal.confidence >= 0.75:
            size = self.bet_size * 1.5  # Strong signal = 1.5x bet
        elif signal.confidence >= 0.60:
            size = self.bet_size
        else:
            size = self.bet_size * 0.5  # Weak signal = half bet
        
        # Cap at available bankroll
        if self._trading_state:
            max_bet = self._trading_state.bankroll * 0.10  # Max 10% per directional bet
            size = min(size, max_bet)
        
        bet = {
            "market_slug": market.slug,
            "window_ts": self._current_window_ts,
            "side": buy_side,
            "price": buy_price,
            "size": size,
            "signal": signal,
            "placed_at": time.time(),
            "settled": False,
            "pnl": 0.0,
        }
        
        self.active_bets.append(bet)
        self.stats.trades_taken += 1
        
        msg = (f"[MOMENTUM] 💰 BET: {buy_side}@{buy_price:.4f} "
               f"${size:.2f} on {market.slug} "
               f"(momentum: {signal.price_change_pct:+.4f}%)")
        print(msg)
        
        with open("/tmp/spread_trades.log", "a") as _f:
            _f.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:12]} {msg}\n")
            _f.flush()
        
        return bet
    
    def settle_bet(self, bet: dict, outcome: str):
        """Settle a directional bet after market resolution."""
        if bet.get("settled"):
            return
        
        bet["settled"] = True
        side = bet["side"]
        price = bet["price"]
        size = bet["size"]
        
        # Number of shares bought
        shares = size / price
        
        if (side == "YES" and outcome == "up") or (side == "NO" and outcome == "down"):
            # Won — shares pay $1 each
            payout = shares * 1.0
            bet["pnl"] = payout - size
            self.stats.wins += 1
            self.stats.biggest_win = max(self.stats.biggest_win, bet["pnl"])
            emoji = "✅"
        else:
            # Lost — shares worth $0
            bet["pnl"] = -size
            self.stats.losses += 1
            self.stats.biggest_loss = min(self.stats.biggest_loss, bet["pnl"])
            emoji = "❌"
        
        self.stats.total_pnl += bet["pnl"]
        self.active_bets.remove(bet)
        self.completed_bets.append(bet)
        
        # Update bankroll
        if self._trading_state:
            self._trading_state.bankroll += bet["pnl"]
        
        msg = (f"[MOMENTUM] {emoji} SETTLED: {side} on {bet['market_slug']} "
               f"| Outcome: {outcome} | PnL: ${bet['pnl']:+.2f} "
               f"| Record: {self.stats.wins}W/{self.stats.losses}L "
               f"({self.stats.win_rate:.1f}%)")
        print(msg)
        
        with open("/tmp/spread_trades.log", "a") as _f:
            _f.write(f"{datetime.now().strftime('%H:%M:%S.%f')[:12]} {msg}\n")
            _f.flush()
    
    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()
    
    def get_stats(self) -> dict:
        return {
            "active_bets": len(self.active_bets),
            "completed_bets": len(self.completed_bets),
            "config": {
                "bet_size": self.bet_size,
                "min_move_pct": f"{self.min_move_pct}%",
                "strong_move_pct": f"{self.strong_move_pct}%",
                "entry_window": f"{self.entry_window_start}-{self.entry_window_end}s",
            },
            **self.stats.to_dict(),
        }
