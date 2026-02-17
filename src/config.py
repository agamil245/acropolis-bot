"""Configuration management for AcropolisBot.

Comprehensive config with all market definitions, strategy parameters,
risk management settings, and API configuration.
"""

import os
import math
from datetime import timezone, timedelta
from enum import Enum
from dotenv import load_dotenv

load_dotenv()


class RiskLevel(Enum):
    """Risk level presets affecting Kelly fraction, exposure, and drawdown limits."""
    CONSERVATIVE = "conservative"
    MODERATE = "moderate"
    AGGRESSIVE = "aggressive"


class MarketType(Enum):
    """Supported market types on Polymarket."""
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

    @property
    def slug_prefix(self) -> str:
        """Return the slug prefix used in Polymarket API."""
        return self.value

    @property
    def display_name(self) -> str:
        """Human-readable market name."""
        asset = self.asset
        interval = "5m" if "5m" in self.value else "15m"
        return f"{asset} {interval}"


# ===== TIMEZONE CONFIGURATION =====
TIMEZONE_NAME = os.getenv("TIMEZONE", "America/New_York")

_TZ_OFFSETS = {
    "America/New_York": timedelta(hours=-5),
    "America/Chicago": timedelta(hours=-6),
    "America/Denver": timedelta(hours=-7),
    "America/Los_Angeles": timedelta(hours=-8),
    "Europe/London": timedelta(hours=0),
    "Europe/Paris": timedelta(hours=1),
    "Europe/Berlin": timedelta(hours=1),
    "Asia/Tokyo": timedelta(hours=9),
    "Asia/Singapore": timedelta(hours=8),
    "Asia/Jakarta": timedelta(hours=7),
    "Asia/Shanghai": timedelta(hours=8),
    "Asia/Kolkata": timedelta(hours=5, minutes=30),
    "Australia/Sydney": timedelta(hours=11),
    "UTC": timedelta(hours=0),
}

LOCAL_TZ = timezone(_TZ_OFFSETS.get(TIMEZONE_NAME, timedelta(hours=-5)))


# ===== MARKET CHARACTERISTICS =====
# Historical base rates and volatility profiles for each market type
# Used by strategies for baseline expectations
MARKET_PROFILES = {
    MarketType.BTC_5M: {
        "base_up_rate": 0.50,       # ~50% of 5m candles are up
        "streak_reversal_4": 0.62,  # Historical reversal rate after 4-streak
        "streak_reversal_5": 0.65,  # After 5-streak
        "streak_reversal_6": 0.68,  # After 6-streak
        "avg_spread": 0.02,         # Average bid-ask spread
        "avg_volume_usd": 5000,     # Average market volume
        "resolution_delay_s": 45,   # Average seconds to resolve after window closes
    },
    MarketType.BTC_15M: {
        "base_up_rate": 0.50,
        "streak_reversal_4": 0.60,
        "streak_reversal_5": 0.63,
        "streak_reversal_6": 0.66,
        "avg_spread": 0.025,
        "avg_volume_usd": 8000,
        "resolution_delay_s": 60,
    },
    MarketType.ETH_5M: {
        "base_up_rate": 0.50,
        "streak_reversal_4": 0.61,
        "streak_reversal_5": 0.64,
        "streak_reversal_6": 0.67,
        "avg_spread": 0.025,
        "avg_volume_usd": 3000,
        "resolution_delay_s": 50,
    },
    MarketType.ETH_15M: {
        "base_up_rate": 0.50,
        "streak_reversal_4": 0.59,
        "streak_reversal_5": 0.62,
        "streak_reversal_6": 0.65,
        "avg_spread": 0.03,
        "avg_volume_usd": 5000,
        "resolution_delay_s": 65,
    },
    MarketType.SOL_5M: {
        "base_up_rate": 0.50,
        "streak_reversal_4": 0.60,
        "streak_reversal_5": 0.63,
        "streak_reversal_6": 0.66,
        "avg_spread": 0.03,
        "avg_volume_usd": 2000,
        "resolution_delay_s": 55,
    },
    MarketType.SOL_15M: {
        "base_up_rate": 0.50,
        "streak_reversal_4": 0.58,
        "streak_reversal_5": 0.61,
        "streak_reversal_6": 0.64,
        "avg_spread": 0.035,
        "avg_volume_usd": 3500,
        "resolution_delay_s": 70,
    },
}


class Config:
    """Global configuration for AcropolisBot.

    All settings are loaded from environment variables with sensible defaults.
    Risk level presets affect multiple parameters simultaneously.
    """

    # ===== TELEGRAM NOTIFICATIONS =====
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
    TELEGRAM_PNL_INTERVAL: int = int(os.getenv("TELEGRAM_PNL_INTERVAL", "300"))  # seconds

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
    ENABLE_STREAK: bool = os.getenv("ENABLE_STREAK", "false").lower() == "true"
    ENABLE_COPYTRADE: bool = os.getenv("ENABLE_COPYTRADE", "false").lower() == "true"
    ENABLE_SELECTIVE: bool = os.getenv("ENABLE_SELECTIVE", "false").lower() == "true"
    ENABLE_PANIC_REVERSAL: bool = os.getenv("ENABLE_PANIC_REVERSAL", "true").lower() == "true"

    # ===== PANIC REVERSAL (Layer 4) =====
    PANIC_MAX_ENTRY_PRICE: float = float(os.getenv("PANIC_MAX_ENTRY_PRICE", "0.10"))
    PANIC_BET_SIZE: float = float(os.getenv("PANIC_BET_SIZE", "3.0"))
    PANIC_MAX_CONCURRENT: int = int(os.getenv("PANIC_MAX_CONCURRENT", "3"))
    PANIC_MAX_DAILY_SPEND: float = float(os.getenv("PANIC_MAX_DAILY_SPEND", "50.0"))
    PANIC_MIN_TIME_LEFT: int = int(os.getenv("PANIC_MIN_TIME_LEFT", "60"))
    PANIC_TAKE_PROFIT_MULTIPLIER: float = float(os.getenv("PANIC_TAKE_PROFIT_MULTIPLIER", "3.0"))

    # ===== ARBITRAGE SETTINGS =====
    ARB_THRESHOLD: float = float(os.getenv("ARB_THRESHOLD", "0.98"))
    ARB_MIN_EDGE_PCT: float = float(os.getenv("ARB_MIN_EDGE_PCT", "0.5"))
    ARB_MAX_EXPOSURE: float = float(os.getenv("ARB_MAX_EXPOSURE", "100"))
    ARB_CHECK_INTERVAL: float = float(os.getenv("ARB_CHECK_INTERVAL", "0.1"))
    ARB_MIN_BET: float = float(os.getenv("ARB_MIN_BET", "5"))

    # ===== STREAK SETTINGS =====
    STREAK_TRIGGER: int = int(os.getenv("STREAK_TRIGGER", "3"))
    STREAK_MIN_REVERSAL_RATE: float = float(os.getenv("STREAK_MIN_REVERSAL_RATE", "0.60"))
    STREAK_USE_MARKET_PROFILE: bool = os.getenv("STREAK_USE_MARKET_PROFILE", "true").lower() == "true"

    # ===== COPYTRADE SETTINGS =====
    COPY_WALLETS: list[str] = [
        w.strip() for w in os.getenv("COPY_WALLETS", "").split(",") if w.strip()
    ]
    COPY_POLL_INTERVAL: float = float(os.getenv("COPY_POLL_INTERVAL", "1.5"))
    FAST_POLL_INTERVAL: float = float(os.getenv("FAST_POLL_INTERVAL", "1.5"))
    COPY_ONLY_BUYS: bool = os.getenv("COPY_ONLY_BUYS", "true").lower() == "true"

    # ===== SELECTIVE COPYTRADE FILTER =====
    SELECTIVE_FILTER: bool = os.getenv("SELECTIVE_FILTER", "false").lower() == "true"
    SELECTIVE_MAX_DELAY_MS: int = int(os.getenv("SELECTIVE_MAX_DELAY_MS", "20000"))
    SELECTIVE_MIN_FILL_PRICE: float = float(os.getenv("SELECTIVE_MIN_FILL_PRICE", "0.55"))
    SELECTIVE_MAX_FILL_PRICE: float = float(os.getenv("SELECTIVE_MAX_FILL_PRICE", "0.80"))
    SELECTIVE_MAX_PRICE_MOVEMENT_PCT: float = float(os.getenv("SELECTIVE_MAX_PRICE_MOVEMENT_PCT", "15.0"))
    SELECTIVE_MAX_SPREAD: float = float(os.getenv("SELECTIVE_MAX_SPREAD", "0.025"))
    SELECTIVE_MAX_VOLATILITY_FACTOR: float = float(os.getenv("SELECTIVE_MAX_VOLATILITY_FACTOR", "1.25"))
    SELECTIVE_MIN_DEPTH_AT_BEST: float = float(os.getenv("SELECTIVE_MIN_DEPTH_AT_BEST", "5.0"))

    # ===== BANKROLL & COMPOUNDING =====
    INITIAL_BANKROLL: float = float(os.getenv("INITIAL_BANKROLL", "100.0"))
    _risk_level_str = os.getenv("RISK_LEVEL", "moderate").lower()
    RISK_LEVEL: RiskLevel = (
        RiskLevel(_risk_level_str)
        if _risk_level_str in [r.value for r in RiskLevel]
        else RiskLevel.MODERATE
    )
    KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))
    AUTO_COMPOUND: bool = os.getenv("AUTO_COMPOUND", "true").lower() == "true"
    BET_AMOUNT: float = float(os.getenv("BET_AMOUNT", "5"))  # Fixed bet amount mode
    MIN_BET: float = float(os.getenv("MIN_BET", "5"))
    MAX_BET: float = float(os.getenv("MAX_BET", "100"))
    USE_KELLY: bool = os.getenv("USE_KELLY", "true").lower() == "true"

    # ===== RISK MANAGEMENT =====
    MAX_DAILY_BETS: int = int(os.getenv("MAX_DAILY_BETS", "100"))
    MAX_DAILY_LOSS: float = float(os.getenv("MAX_DAILY_LOSS", "100"))
    DRAWDOWN_THRESHOLD: float = float(os.getenv("DRAWDOWN_THRESHOLD", "0.20"))
    MAX_POSITION_SIZE_PCT: float = float(os.getenv("MAX_POSITION_SIZE_PCT", "0.10"))  # Max 10% of bankroll per trade
    MAX_CONCURRENT_POSITIONS: int = int(os.getenv("MAX_CONCURRENT_POSITIONS", "10"))
    CIRCUIT_BREAKER_LOSSES: int = int(os.getenv("CIRCUIT_BREAKER_LOSSES", "5"))
    COOLDOWN_MINUTES: int = int(os.getenv("COOLDOWN_MINUTES", "30"))

    # ===== TIMING =====
    ENTRY_SECONDS_BEFORE: int = int(os.getenv("ENTRY_SECONDS_BEFORE", "120"))
    SETTLEMENT_CHECK_INTERVAL: float = float(os.getenv("SETTLEMENT_CHECK_INTERVAL", "10"))

    # ===== API SETTINGS =====
    USE_WEBSOCKET: bool = os.getenv("USE_WEBSOCKET", "true").lower() == "true"
    REST_TIMEOUT: float = float(os.getenv("REST_TIMEOUT", "3"))
    REST_RETRIES: int = int(os.getenv("REST_RETRIES", "2"))
    WS_RECONNECT_DELAY: int = int(os.getenv("WS_RECONNECT_DELAY", "5"))
    WS_CLOB_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    WS_RTDS_URL = "wss://ws-live-data.polymarket.com"
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = int(os.getenv("RATE_LIMIT_REQUESTS_PER_MINUTE", "120"))

    # ===== DELAY IMPACT MODEL =====
    DELAY_MODEL_BASE_COEF: float = float(os.getenv("DELAY_MODEL_BASE_COEF", "0.8"))
    DELAY_MODEL_MAX_IMPACT: float = float(os.getenv("DELAY_MODEL_MAX_IMPACT", "10.0"))
    DELAY_MODEL_BASELINE_SPREAD: float = float(os.getenv("DELAY_MODEL_BASELINE_SPREAD", "0.02"))

    # ===== BINANCE (Latency Arb) =====
    BINANCE_WS_URL: str = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443")
    BINANCE_API_KEY: str = os.getenv("BINANCE_API_KEY", "")  # optional, for higher rate limits

    # ===== SPREAD FARMING (Layer 1) =====
    SPREAD_OFFSET: float = float(os.getenv("SPREAD_OFFSET", "0.02"))  # bid this far below mid
    SPREAD_ORDER_SIZE: float = float(os.getenv("SPREAD_ORDER_SIZE", "25.0"))  # USD per leg
    SPREAD_REFRESH_INTERVAL: float = float(os.getenv("SPREAD_REFRESH_INTERVAL", "15.0"))  # seconds

    # ===== LATENCY ARB (Layer 2) =====
    MOMENTUM_THRESHOLD_PCT: float = float(os.getenv("MOMENTUM_THRESHOLD_PCT", "0.20"))  # 0.20%
    MOMENTUM_WINDOW_SECONDS: float = float(os.getenv("MOMENTUM_WINDOW_SECONDS", "2.0"))
    LATENCY_MAX_POSITION: float = float(os.getenv("LATENCY_MAX_POSITION", "50.0"))  # max USD per snipe
    MIN_PRICE_GAP: float = float(os.getenv("MIN_PRICE_GAP", "0.05"))  # min Poly lag to fire

    # ===== CHAINLINK ORACLE (Layer 2+: THE edge) =====
    CHAINLINK_RPC_URL: str = os.getenv("CHAINLINK_RPC_URL", "https://polygon-rpc.com")
    CHAINLINK_POLL_INTERVAL: float = float(os.getenv("CHAINLINK_POLL_INTERVAL", "1.0"))
    CHAINLINK_MIN_DIVERGENCE: float = float(os.getenv("CHAINLINK_MIN_DIVERGENCE", "0.03"))  # 3¢
    CHAINLINK_MIN_MOMENTUM_PCT: float = float(os.getenv("CHAINLINK_MIN_MOMENTUM_PCT", "0.5"))
    CHAINLINK_MIN_TIME_LEFT: int = int(os.getenv("CHAINLINK_MIN_TIME_LEFT", "60"))  # seconds

    # ===== POLYGONSCAN =====
    POLYGONSCAN_API_KEY: str = os.getenv("POLYGONSCAN_API_KEY", "")

    # ===== WEB GUI =====
    WEB_PORT: int = int(os.getenv("WEB_PORT", "8080"))
    WEB_HOST: str = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_SECRET_KEY: str = os.getenv("WEB_SECRET_KEY", "change_me_in_production")

    # ===== PAPER TRADING (Independent Mode) =====
    PAPER_INITIAL_BANKROLL: float = float(os.getenv("PAPER_INITIAL_BANKROLL", "1000.0"))
    PAPER_LOG_FILE: str = os.getenv("PAPER_LOG_FILE", "paper_trades.json")

    # ===== FILES =====
    LOG_FILE: str = "acropolis.log"
    TRADES_FILE: str = "trades.json"
    STATE_FILE: str = "bot_state.json"
    HISTORY_FILE: str = "trade_history_full.json"

    # ===== LOG LEVEL =====
    LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    # ===== RISK LEVEL MAPPINGS =====
    @staticmethod
    def get_kelly_fraction_for_risk() -> float:
        """Get Kelly fraction based on risk level.

        Conservative: 1/8 Kelly (very safe, slow growth)
        Moderate: 1/4 Kelly (balanced risk/reward)
        Aggressive: 1/2 Kelly (faster growth, higher variance)
        """
        mapping = {
            RiskLevel.CONSERVATIVE: 0.125,
            RiskLevel.MODERATE: 0.25,
            RiskLevel.AGGRESSIVE: 0.5,
        }
        return mapping.get(Config.RISK_LEVEL, 0.25)

    @staticmethod
    def get_max_exposure_for_risk() -> float:
        """Get max single-trade exposure as fraction of bankroll."""
        mapping = {
            RiskLevel.CONSERVATIVE: 0.05,   # 5% of bankroll
            RiskLevel.MODERATE: 0.10,       # 10% of bankroll
            RiskLevel.AGGRESSIVE: 0.20,     # 20% of bankroll
        }
        return mapping.get(Config.RISK_LEVEL, 0.10)

    @staticmethod
    def get_drawdown_threshold_for_risk() -> float:
        """Get drawdown threshold (pause trading) based on risk level."""
        mapping = {
            RiskLevel.CONSERVATIVE: 0.10,   # Pause at 10% drawdown
            RiskLevel.MODERATE: 0.20,       # Pause at 20% drawdown
            RiskLevel.AGGRESSIVE: 0.35,     # Pause at 35% drawdown
        }
        return mapping.get(Config.RISK_LEVEL, 0.20)

    @staticmethod
    def get_circuit_breaker_for_risk() -> int:
        """Get consecutive loss limit before circuit breaker."""
        mapping = {
            RiskLevel.CONSERVATIVE: 3,
            RiskLevel.MODERATE: 5,
            RiskLevel.AGGRESSIVE: 8,
        }
        return mapping.get(Config.RISK_LEVEL, 5)

    @classmethod
    def get_market_profile(cls, market_type: MarketType) -> dict:
        """Get the historical profile for a market type."""
        return MARKET_PROFILES.get(market_type, MARKET_PROFILES[MarketType.BTC_5M])

    @classmethod
    def validate(cls) -> list[str]:
        """Validate configuration and return list of warnings."""
        warnings = []

        if not cls.PAPER_TRADE and not cls.PRIVATE_KEY:
            warnings.append("CRITICAL: Live trading enabled but no PRIVATE_KEY set!")

        if cls.SIGNATURE_TYPE == 1 and not cls.FUNDER_ADDRESS:
            warnings.append("CRITICAL: Proxy wallet (SIGNATURE_TYPE=1) requires FUNDER_ADDRESS")

        if cls.MIN_BET >= cls.MAX_BET:
            warnings.append(f"MIN_BET (${cls.MIN_BET}) >= MAX_BET (${cls.MAX_BET})")

        if cls.INITIAL_BANKROLL < cls.MIN_BET:
            warnings.append(f"INITIAL_BANKROLL (${cls.INITIAL_BANKROLL}) < MIN_BET (${cls.MIN_BET})")

        if cls.KELLY_FRACTION > 1.0:
            warnings.append(f"KELLY_FRACTION ({cls.KELLY_FRACTION}) > 1.0 (full Kelly is dangerous!)")

        if cls.DRAWDOWN_THRESHOLD > 0.5:
            warnings.append(f"DRAWDOWN_THRESHOLD ({cls.DRAWDOWN_THRESHOLD:.0%}) is very high")

        if not cls.ACTIVE_MARKETS:
            warnings.append("No active markets configured")

        if cls.ENABLE_COPYTRADE and not cls.COPY_WALLETS:
            warnings.append("Copytrade enabled but no COPY_WALLETS configured")

        if cls.MAX_DAILY_LOSS > cls.INITIAL_BANKROLL:
            warnings.append(f"MAX_DAILY_LOSS (${cls.MAX_DAILY_LOSS}) > INITIAL_BANKROLL (${cls.INITIAL_BANKROLL})")

        return warnings

    @classmethod
    def print_summary(cls):
        """Print configuration summary to console."""
        mode = "PAPER" if cls.PAPER_TRADE else "LIVE"
        risk = cls.RISK_LEVEL.value.upper()
        markets = ", ".join(m.display_name for m in cls.ACTIVE_MARKETS)
        strategies = []
        if cls.ENABLE_ARBITRAGE:
            strategies.append("Arbitrage")
        if cls.ENABLE_STREAK:
            strategies.append("Streak")
        if cls.ENABLE_COPYTRADE:
            strategies.append("Copytrade")
        if cls.ENABLE_SELECTIVE:
            strategies.append("Selective")

        print("╔══════════════════════════════════════════╗")
        print("║       AcropolisBot Configuration         ║")
        print("╠══════════════════════════════════════════╣")
        print(f"║  Mode:       {mode:<28}║")
        print(f"║  Risk:       {risk:<28}║")
        print(f"║  Markets:    {markets:<28}║")
        print(f"║  Strategies: {', '.join(strategies):<28}║")
        print(f"║  Bankroll:   ${cls.INITIAL_BANKROLL:<27.2f}║")
        print(f"║  Kelly:      {cls.get_kelly_fraction_for_risk():<28}║")
        print(f"║  Max Bet:    ${cls.MAX_BET:<27.2f}║")
        print(f"║  Drawdown:   {cls.get_drawdown_threshold_for_risk():.0%}{'':<24}║")
        print(f"║  WebSocket:  {'ON' if cls.USE_WEBSOCKET else 'OFF':<28}║")
        print("╚══════════════════════════════════════════╝")

        # Print warnings
        warnings = cls.validate()
        if warnings:
            print("\n⚠️  Configuration Warnings:")
            for w in warnings:
                print(f"  • {w}")
            print()
