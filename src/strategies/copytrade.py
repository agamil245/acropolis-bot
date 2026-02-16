"""Copytrade strategy - monitor profitable wallets."""

import re
import time
from dataclasses import dataclass
from typing import Optional

import requests

from src.config import Config, MarketType


@dataclass
class CopySignal:
    """Signal when a tracked wallet makes a trade."""
    
    wallet: str
    direction: str  # "up" or "down"
    market_ts: int
    trade_ts: int
    price: float
    usdc_amount: float
    trader_name: str
    market_type: MarketType
    delay_ms: int = 0


class CopytradeMonitor:
    """Monitor wallets for trades across all market types."""

    # Pattern: {asset}-updown-{interval}-{timestamp}
    MARKET_PATTERN = re.compile(r"^(btc|eth|sol)-updown-(5m|15m)-(\d+)$")

    def __init__(self, wallets: Optional[list[str]] = None):
        self.wallets = wallets or Config.COPY_WALLETS
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "AcropolisBot/1.0",
            "Accept": "application/json"
        })
        self.last_seen: dict[str, int] = {w: int(time.time()) for w in self.wallets}

    def _fetch_activity(self, wallet: str, limit: int = 10) -> list[dict]:
        """Fetch recent activity for a wallet."""
        try:
            resp = self.session.get(
                f"{Config.DATA_API}/activity",
                params={"user": wallet, "limit": limit, "offset": 0},
                timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            print(f"[copytrade] Error fetching activity: {e}")
            return []

    def _parse_market_slug(self, slug: str) -> Optional[tuple[MarketType, int]]:
        """Parse market slug to extract type and timestamp."""
        match = self.MARKET_PATTERN.match(slug)
        if not match:
            return None

        asset = match.group(1)
        interval = match.group(2)
        timestamp = int(match.group(3))

        # Build market type key
        key = f"{asset.upper()}_{interval.upper()}"
        
        try:
            market_type = MarketType[key]
            return (market_type, timestamp)
        except KeyError:
            return None

    def _trade_to_signal(self, trade: dict) -> Optional[CopySignal]:
        """Convert API trade to CopySignal."""
        slug = trade.get("slug", "")
        parsed = self._parse_market_slug(slug)
        if not parsed:
            return None

        market_type, market_ts = parsed
        
        # Check if this market type is enabled
        if market_type not in Config.ACTIVE_MARKETS:
            return None

        direction = trade.get("outcome", "").lower()
        if direction not in ["up", "down"]:
            return None

        trade_ts = trade.get("timestamp", 0)
        now_ms = int(time.time() * 1000)
        delay_ms = now_ms - trade_ts

        return CopySignal(
            wallet=trade.get("proxyWallet", ""),
            direction=direction,
            market_ts=market_ts,
            trade_ts=trade_ts,
            price=float(trade.get("price", 0.5)),
            usdc_amount=float(trade.get("usdcSize", 0)),
            trader_name=trade.get("pseudonym", trade.get("name", "")[:10]),
            market_type=market_type,
            delay_ms=delay_ms
        )

    def poll(self) -> list[CopySignal]:
        """Poll all tracked wallets for new trades."""
        signals: list[CopySignal] = []

        for wallet in self.wallets:
            activity = self._fetch_activity(wallet)
            last_ts = self.last_seen.get(wallet, 0)
            new_last_ts = last_ts

            for trade in activity:
                trade_ts = trade.get("timestamp", 0)
                trade_type = trade.get("type", "")

                if trade_ts <= last_ts or trade_type != "TRADE":
                    continue

                signal = self._trade_to_signal(trade)
                if signal:
                    signals.append(signal)
                    new_last_ts = max(new_last_ts, trade_ts)

            self.last_seen[wallet] = new_last_ts

        return signals
