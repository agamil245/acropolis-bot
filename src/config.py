"""Configuration management for AcropolisBot."""

import os
from datetime import timezone, timedelta
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class RiskLevel(Enum):
    """Risk level presets."""
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class MarketType(Enum):
    """Supported market types."""
    BTC_5M = "btc-updown-5m"
    BTC_15M = "btc-updown-15m"
    ETH_5M = "eth-updown-5m"
    ETH_15M = "eth-updown-15m"
    SOL_5M = "sol-updown-5m"
    SOL_15M = "sol-updown-15m"

    @property
    def interval_seconds(self) -> int:
        """Return the market interval in seconds."""
        if "5m" in self.value:
            return 300  # 5 minutes
        elif "15m" in self.value:
            return 900  # 15 minutes
        return 300

    @property
    def asset(self) -> str:
        """Return the asset (BTC, ETH, SOL)."""
        return self.value.split("-")[0].upper()


# Timezone configuration
TIMEZONE_NAME = os.getenv("TIMEZONE", "America/New_York")

_TZ_OFFSETS = {
    "America/New_York": timedelta(hours=-5),
    "America/Chicago": timedelta(hours=-6),
    "America/Los_Angeles": timedelta(hours=-8),
    "Europe/London": timedelta(hours=0),
    "Europe/Paris": timedelta(hours=1),
    "Asia/Tokyo": timedelta(hours=9),
    "Asia/Singapore": timedelta(hours=8),
    "Asia/Jakarta": timedelta(hours=7),
    "UTC": timedelta(hours=0),
}

LOCAL_TZ = timezone(_TZ_OFFSETS.get(TIMEZONE_NAME, timedelta(hours=-5)))


class Config:
    """Global configuration for AcropolisBot."""
    
    # ===== WALLET =====
    PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
    FUNDER_ADDRESS: str = os.getenv("FUNDER_ADDRESS", "")
    SIGNATURE_TYPE: int = int(os.getenv("SIGNATURE_TYPE", "0"))

    # ===== POLYMARKET APIs =====
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API = "https://clob.polymarket.com"
    DATA_API = "https://data-api.polymarket.com"
    CHAIN_ID = 137  # Polygon mainnet

    # ===== MODE =====
    PAPER_TRADE: bool = os.getenv("PAPER_TRADE", "true").lower() == "true"

    # ===== MARKETS =====
    _active_markets_str = os.getenv("ACTIVE_MARKETS", "BTC_5M,ETH_5M,SOL_5M")
    ACTIVE_MARKETS: list[MarketType] = [
        MarketType[m.strip()]
        for m in _active_markets_str.split(",")
        if m.strip() and m.strip() in MarketType.__members__
    ]
    if not ACTIVE_MARKETS:
        ACTIVE_MARKETS = [MarketType.BTC_5M]

    # ===== STRATEGIES =====
    ENABLE_ARBITRAGE: bool = os.getenv("ENABLE_ARBITRAGE", "true").lower() == "true"
    ENABLE_STREAK: bool = os.getenv("ENABLE_STREAK", "true").lower() == "true"
    ENABLE_COPYTRADE: bool = os.getenv("ENABLE_COPYTRADE", "false").lower() == "true"
    ENABLE_SELECTIVE: bool = os.getenv("ENABLE_SELECTIVE", "false").lower() == "true"

    # ===== ARBITRAGE SETTINGS =====
    ARB_THRESHOLD: float = float(os.getenv("ARB_THRESHOLD", "0.98"))
    ARB_MIN_EDGE_PCT: float = float(os.getenv("ARB_MIN_EDGE_PCT", "0.5"))
    ARB_MAX_EXPOSURE: float = float(os.getenv("ARB_MAX_EXPOSURE", "100"))
    ARB_CHECK_INTERVAL: float = float(os.getenv("ARB_CHECK_INTERVAL", "0.1"))
    ARB_MIN_BET: float = float(os.getenv("ARB_MIN_BET", "5"))

    # ===== STREAK SETTINGS =====
    STREAK_TRIGGER: int = int(os.getenv("STREAK_TRIGGER", "4"))
    STREAK_MIN_REVERSAL_RATE: float = float(os.getenv("STREAK_MIN_REVERSAL_RATE", "0.60"))

    # ===== COPYTRADE SETTINGS =====
    COPY_WALLETS: list[str] = [
        w.strip() for w in os.getenv("COPY_WALLETS", "").split(",") if w.strip()
    ]
    COPY_POLL_INTERVAL: float = float(os.getenv("COPY_POLL_INTERVAL", "1.5"))
    SELECTIVE_FILTER: bool = os.getenv("SELECTIVE_FILTER", "false").lower() == "true"
    SELECTIVE_MAX_DELAY_MS: int = int(os.getenv("SELECTIVE_MAX_DELAY_MS", "20000"))
    SELECTIVE_MIN_FILL_PRICE: float = float(os.getenv("SELECTIVE_MIN_FILL_PRICE", "0.55"))
    SELECTIVE_MAX_FILL_PRICE: float = float(os.getenv("SELECTIVE_MAX_FILL_PRICE", "0.80"))
    SELECTIVE_MAX_SPREAD: float = float(os.getenv("SELECTIVE_MAX_SPREAD", "0.025"))

    # ===== BANKROLL & COMPOUNDING =====
    INITIAL_BANKROLL: float = float(os.getenv("INITIAL_BANKROLL", "100.0"))
    _risk_level_str = os.getenv("RISK_LEVEL", "moderate").lower()
    RISK_LEVEL: RiskLevel = RiskLevel(_risk_level_str) if _risk_level_str in [r.value for r in RiskLevel] else RiskLevel.MODERATE
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
    AUTO_COMPOUND: bool = os.getenv("AUTO_COMPOUND", "true").lower() == "true"
    MIN_BET: float = float(os.getenv("MIN_BET", "5"))
    MAX_BET: float = float(os.getenv("MAX_BET", "100"))

    # ===== RISK MANAGEMENT =====
    MAX_DAILY_BETS: int = int(os.getenv("MAX_DAILY_BETS", "100"))
    MAX_DAILY_LOSS: float = float(os.getenv("MAX_DAILY_LOSS", "100"))
    DRAWDOWN_THRESHOLD: float = float(os.getenv("DRAWDOWN_THRESHOLD", "0.20"))
    CIRCUIT_BREAKER_LOSSES: int = int(os.getenv("CIRCUIT_BREAKER_LOSSES", "5"))
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "30"))

    # ===== TIMING =====
    ENTRY_SECONDS_BEFORE: int = int(os.getenv("ENTRY_SECONDS_BEFORE", "30"))

    # ===== API SETTINGS =====
    USE_WEBSOCKET: bool = os.getenv("USE_WEBSOCKET", "true").lower() == "true"
    REST_TIMEOUT: float = float(os.getenv("REST_TIMEOUT", "3"))
    REST_RETRIES: int = int(os.getenv("REST_RETRIES", "2"))
    WS_RECONNECT_DELAY: int = int(os.getenv("WS_RECONNECT_DELAY", "5"))
    WS_CLOB_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"

    # ===== POLYGONSCAN =====
    POLYGONSCAN_API_KEY: str = os.getenv("POLYGONSCAN_API_KEY", "")

    # ===== WEB GUI =====
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_SECRET_KEY: str = os.getenv("WEB_SECRET_KEY", "change_me_in_production")

    # ===== FILES =====
    LOG_FILE: str = "acropolis.log"
    TRADES_FILE: str = "trades.json"
    STATE_FILE: str = "bot_state.json"

    # ===== RISK LEVEL MAPPINGS =====
    @staticmethod
    def get_kelly_fraction_for_risk() -> float:
        """Get Kelly fraction based on risk level."""
        mapping = {
            RiskLevel.CONSERVATIVE: 0.125,  # 1/8 Kelly
            RiskLevel.MODERATE: 0.25,       # 1/4 Kelly
            RiskLevel.AGGRESSIVE: 0.5,      # 1/2 Kelly
        }
        return mapping.get(Config.RISK_LEVEL, 0.25)

    @staticmethod
    def get_max_exposure_for_risk() -> float:
        """Get max exposure percentage based on risk level."""
        mapping = {
            RiskLevel.CONSERVATIVE: 0.10,   # 10% of bankroll
            RiskLevel.MODERATE: 0.20,       # 20% of bankroll
            RiskLevel.AGGRESSIVE: 0.40,     # 40% of bankroll
        }
        return mapping.get(Config.RISK_LEVEL, 0.20)
