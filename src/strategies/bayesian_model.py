"""
Bayesian Probability Model + Volatility Anomaly Detection + Orderbook Analysis.

The INTELLIGENCE layer for AcropolisBot. Combines:
1. VolatilityTracker — rolling realized vol with anomaly detection
2. BayesianPredictor — posterior probability of UP using multiple evidence streams
3. OrderbookAnalyzer — Polymarket orderbook depth/imbalance/wall detection
4. SignalAggregator — consensus-based signal generation with self-learning weights

Integration API:
  - get_directional_signal(asset, market_type) -> Optional[TradeSignal]
  - get_volatility_regime(asset) -> str
  - should_spread_farm(market) -> bool
  - get_bayesian_probability(asset) -> float
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from src.config import Config, MarketType, MARKET_PROFILES

if TYPE_CHECKING:
    from src.core.polymarket import CachedOrderBook


# ═══════════════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    """Aggregated trade signal emitted when multiple indicators align."""
    asset: str                  # "BTC", "ETH", "SOL"
    direction: str              # "up" or "down"
    confidence: float           # 0-1, how confident the signal is
    bayesian_prob: float        # posterior P(UP)
    volatility_regime: str      # "low", "normal", "high", "extreme"
    recommended_size_pct: float # fraction of Kelly to use (0-1)
    reason: str                 # human-readable explanation
    timestamp: float = 0.0
    momentum_pct: float = 0.0
    orderbook_imbalance: float = 0.0
    volume_imbalance: float = 0.0
    signal_count: int = 0       # how many sub-signals agree

    def __post_init__(self):
        if self.timestamp == 0.0:
            self.timestamp = time.time()


@dataclass
class VolatilitySnapshot:
    """Point-in-time volatility reading."""
    asset: str
    realized_vol_1m: float
    realized_vol_5m: float
    realized_vol_15m: float
    expected_vol: float         # rolling EMA of vol
    z_score: float              # how many stdevs current vol is from expected
    regime: str                 # "low", "normal", "high", "extreme"
    timestamp: float = 0.0


@dataclass
class OrderbookSnapshot:
    """Point-in-time orderbook analysis."""
    bid_depth_usd: float
    ask_depth_usd: float
    imbalance: float            # (bids - asks) / (bids + asks), positive = bullish
    spread: float
    is_thin: bool               # dangerous to trade
    walls: list[dict] = field(default_factory=list)  # [{side, price, size_usd}]
    slippage_10: float = 0.0    # slippage for $10 order
    slippage_50: float = 0.0    # slippage for $50 order


@dataclass
class SignalAccuracy:
    """Tracks accuracy of a signal source for self-learning."""
    name: str
    predictions: int = 0
    correct: int = 0
    weight: float = 1.0

    @property
    def accuracy(self) -> float:
        if self.predictions < 5:
            return 0.5  # not enough data
        return self.correct / self.predictions

    def record(self, predicted_up: bool, actual_up: bool):
        self.predictions += 1
        if predicted_up == actual_up:
            self.correct += 1
        # Adapt weight: better accuracy = higher weight
        if self.predictions >= 10:
            self.weight = 0.5 + self.accuracy  # range 0.5 - 1.5


# ═══════════════════════════════════════════════════════════════════════════════
# VOLATILITY TRACKER
# ═══════════════════════════════════════════════════════════════════════════════

class VolatilityTracker:
    """Track rolling realized volatility from exchange price feeds.

    Calculates realized vol over 1m, 5m, 15m windows using log returns.
    Detects anomalies by comparing current vol to an EMA baseline.

    Vol regimes:
      - "low"     : z < -1.0  → range-bound, spread farming territory
      - "normal"  : -1.0 <= z <= 1.0
      - "high"    : 1.0 < z <= 2.5 → directional moves likely
      - "extreme" : z > 2.5 → danger zone, pause market-making
    """

    # Window sizes in seconds
    WINDOWS = {"1m": 60, "5m": 300, "15m": 900}

    # Z-score thresholds for regime classification
    Z_LOW = -1.0
    Z_HIGH = 1.0
    Z_EXTREME = 2.5

    def __init__(self, ema_alpha: float = 0.02):
        # Price buffers: asset -> deque of (timestamp, price)
        self._prices: dict[str, deque] = {}
        # EMA of 1m realized vol: asset -> float
        self._vol_ema: dict[str, float] = {}
        # EMA of vol-of-vol (for z-score denominator)
        self._vol_std_ema: dict[str, float] = {}
        self._ema_alpha = ema_alpha
        # Latest snapshot per asset
        self._snapshots: dict[str, VolatilitySnapshot] = {}

    def _ensure_asset(self, asset: str):
        if asset not in self._prices:
            self._prices[asset] = deque(maxlen=50000)
            self._vol_ema[asset] = 0.0
            self._vol_std_ema[asset] = 0.0

    def on_price(self, asset: str, price: float, timestamp: Optional[float] = None):
        """Feed a new price tick. Called on every exchange trade."""
        self._ensure_asset(asset)
        ts = timestamp or time.time()
        self._prices[asset].append((ts, price))

    def _calc_realized_vol(self, asset: str, window_seconds: int) -> float:
        """Calculate annualized realized volatility over a window using log returns."""
        prices = self._prices.get(asset)
        if not prices or len(prices) < 3:
            return 0.0

        now = prices[-1][0]
        cutoff = now - window_seconds

        # Collect prices in window at ~1s intervals
        sampled = []
        last_ts = 0
        for ts, p in prices:
            if ts < cutoff:
                continue
            if ts - last_ts >= 0.5:  # at least 0.5s apart
                sampled.append(p)
                last_ts = ts

        if len(sampled) < 3:
            return 0.0

        # Log returns
        log_returns = []
        for i in range(1, len(sampled)):
            if sampled[i - 1] > 0 and sampled[i] > 0:
                lr = math.log(sampled[i] / sampled[i - 1])
                log_returns.append(lr)

        if len(log_returns) < 2:
            return 0.0

        # Standard deviation of log returns
        mean_lr = sum(log_returns) / len(log_returns)
        variance = sum((r - mean_lr) ** 2 for r in log_returns) / (len(log_returns) - 1)
        std = math.sqrt(variance)

        # Annualize: scale by sqrt(periods_per_year)
        # If sampling ~1s intervals, periods_per_year ≈ 365*24*3600
        avg_interval = window_seconds / max(len(log_returns), 1)
        if avg_interval <= 0:
            return 0.0
        periods_per_year = 365.25 * 86400 / avg_interval
        annualized = std * math.sqrt(periods_per_year)

        return annualized

    def update(self, asset: str) -> Optional[VolatilitySnapshot]:
        """Recalculate volatility snapshot for an asset. Call periodically (~1s)."""
        self._ensure_asset(asset)
        prices = self._prices.get(asset)
        if not prices or len(prices) < 10:
            return None

        vol_1m = self._calc_realized_vol(asset, self.WINDOWS["1m"])
        vol_5m = self._calc_realized_vol(asset, self.WINDOWS["5m"])
        vol_15m = self._calc_realized_vol(asset, self.WINDOWS["15m"])

        # Use 1m vol as the "current" vol for anomaly detection
        current_vol = vol_1m

        # Update EMA of vol
        alpha = self._ema_alpha
        prev_ema = self._vol_ema[asset]
        if prev_ema == 0.0:
            self._vol_ema[asset] = current_vol
            self._vol_std_ema[asset] = current_vol * 0.3  # initial estimate
        else:
            self._vol_ema[asset] = alpha * current_vol + (1 - alpha) * prev_ema
            deviation = abs(current_vol - self._vol_ema[asset])
            self._vol_std_ema[asset] = alpha * deviation + (1 - alpha) * self._vol_std_ema[asset]

        # Z-score
        expected_vol = self._vol_ema[asset]
        vol_std = self._vol_std_ema[asset]
        if vol_std > 0:
            z_score = (current_vol - expected_vol) / vol_std
        else:
            z_score = 0.0

        # Classify regime
        if z_score < self.Z_LOW:
            regime = "low"
        elif z_score <= self.Z_HIGH:
            regime = "normal"
        elif z_score <= self.Z_EXTREME:
            regime = "high"
        else:
            regime = "extreme"

        snap = VolatilitySnapshot(
            asset=asset,
            realized_vol_1m=vol_1m,
            realized_vol_5m=vol_5m,
            realized_vol_15m=vol_15m,
            expected_vol=expected_vol,
            z_score=z_score,
            regime=regime,
            timestamp=time.time(),
        )
        self._snapshots[asset] = snap
        return snap

    def get_regime(self, asset: str) -> str:
        """Get current volatility regime for an asset."""
        snap = self._snapshots.get(asset)
        if snap and time.time() - snap.timestamp < 30:
            return snap.regime
        # Try to update
        updated = self.update(asset)
        return updated.regime if updated else "normal"

    def get_snapshot(self, asset: str) -> Optional[VolatilitySnapshot]:
        return self._snapshots.get(asset)


# ═══════════════════════════════════════════════════════════════════════════════
# BAYESIAN PREDICTOR
# ═══════════════════════════════════════════════════════════════════════════════

class BayesianPredictor:
    """Bayesian posterior estimation of P(UP) for the current 5-min window.

    Prior: ~50% from market profiles (adjustable per asset).
    Evidence streams update the posterior via likelihood ratios:
      a) Price momentum (30s, 1m, 2m)
      b) Volume imbalance (buy vs sell from trade stream)
      c) Orderbook depth imbalance
      d) Streak pattern (mean reversion)
      e) Volatility regime

    Each evidence source has an adaptive weight based on past accuracy.
    """

    def __init__(self):
        # Per-asset state
        self._posteriors: dict[str, float] = {}  # asset -> P(UP)
        self._last_update: dict[str, float] = {}

        # Evidence caches
        self._buy_volume: dict[str, deque] = {}   # asset -> deque of (ts, volume)
        self._sell_volume: dict[str, deque] = {}
        self._price_history: dict[str, deque] = {}  # asset -> deque of (ts, price)
        self._outcomes: dict[str, list] = {}  # asset -> list of recent "up"/"down"

        # Self-learning accuracy trackers
        self.accuracy: dict[str, SignalAccuracy] = {
            "momentum": SignalAccuracy("momentum"),
            "volume": SignalAccuracy("volume"),
            "orderbook": SignalAccuracy("orderbook"),
            "streak": SignalAccuracy("streak"),
            "volatility": SignalAccuracy("volatility"),
        }

    def _ensure_asset(self, asset: str):
        if asset not in self._posteriors:
            profile = MARKET_PROFILES.get(
                MarketType(f"{asset.lower()}-updown-5m"),
                {"base_up_rate": 0.50}
            )
            self._posteriors[asset] = profile.get("base_up_rate", 0.50)
            self._last_update[asset] = 0.0
            self._buy_volume[asset] = deque(maxlen=5000)
            self._sell_volume[asset] = deque(maxlen=5000)
            self._price_history[asset] = deque(maxlen=20000)
            self._outcomes[asset] = []

    def on_trade(self, asset: str, price: float, volume: float, is_buyer_maker: bool):
        """Feed a trade from the exchange stream."""
        self._ensure_asset(asset)
        ts = time.time()
        self._price_history[asset].append((ts, price))
        if is_buyer_maker:
            self._sell_volume[asset].append((ts, volume * price))
        else:
            self._buy_volume[asset].append((ts, volume * price))

    def on_outcome(self, asset: str, outcome: str):
        """Record a resolved market outcome for streak tracking + accuracy updates."""
        self._ensure_asset(asset)
        self._outcomes[asset].append(outcome)
        if len(self._outcomes[asset]) > 50:
            self._outcomes[asset] = self._outcomes[asset][-50:]

        # Update accuracy of each signal source
        actual_up = (outcome == "up")
        last_posterior = self._posteriors.get(asset, 0.5)
        predicted_up = last_posterior > 0.5

        for tracker in self.accuracy.values():
            tracker.record(predicted_up, actual_up)

    def _momentum_likelihood(self, asset: str) -> float:
        """Likelihood ratio from price momentum.

        Returns L = P(evidence | UP) / P(evidence | DOWN).
        L > 1 means evidence supports UP.
        """
        prices = self._price_history.get(asset)
        if not prices or len(prices) < 5:
            return 1.0

        now = prices[-1][0]
        current_price = prices[-1][1]

        # Calculate momentum over 30s, 1m, 2m
        def price_at_offset(seconds: float) -> Optional[float]:
            cutoff = now - seconds
            for ts, p in reversed(prices):
                if ts <= cutoff:
                    return p
            return None

        momenta = []
        for window in [30, 60, 120]:
            old_price = price_at_offset(window)
            if old_price and old_price > 0:
                pct = (current_price - old_price) / old_price * 100
                momenta.append(pct)

        if not momenta:
            return 1.0

        avg_momentum = sum(momenta) / len(momenta)

        # Convert momentum to likelihood ratio
        # Strong positive momentum -> higher P(UP)
        # Scaling: 0.1% momentum ≈ 1.3x likelihood
        w = self.accuracy["momentum"].weight
        lr = math.exp(avg_momentum * 3.0 * w)  # exponential scaling
        return max(0.3, min(3.5, lr))  # clamp

    def _volume_imbalance_likelihood(self, asset: str) -> float:
        """Likelihood ratio from buy/sell volume imbalance."""
        buy_vol = self._buy_volume.get(asset)
        sell_vol = self._sell_volume.get(asset)
        if not buy_vol and not sell_vol:
            return 1.0

        now = time.time()
        window = 120  # 2 minute window

        total_buy = sum(v for ts, v in buy_vol if now - ts < window) if buy_vol else 0
        total_sell = sum(v for ts, v in sell_vol if now - ts < window) if sell_vol else 0

        total = total_buy + total_sell
        if total < 100:  # not enough volume
            return 1.0

        imbalance = (total_buy - total_sell) / total  # -1 to 1

        w = self.accuracy["volume"].weight
        lr = math.exp(imbalance * 1.5 * w)
        return max(0.4, min(2.5, lr))

    def _orderbook_likelihood(self, orderbook_imbalance: float) -> float:
        """Likelihood ratio from Polymarket orderbook imbalance.

        Positive imbalance (more bids) = bullish.
        """
        if abs(orderbook_imbalance) < 0.05:
            return 1.0

        w = self.accuracy["orderbook"].weight
        lr = math.exp(orderbook_imbalance * 1.2 * w)
        return max(0.5, min(2.0, lr))

    def _streak_likelihood(self, asset: str) -> float:
        """Likelihood ratio from streak pattern (mean reversion signal).

        After N consecutive same outcomes, mean reversion becomes more likely.
        """
        outcomes = self._outcomes.get(asset, [])
        if len(outcomes) < 3:
            return 1.0

        # Count current streak
        last = outcomes[-1]
        streak = 1
        for o in reversed(outcomes[:-1]):
            if o == last:
                streak += 1
            else:
                break

        if streak < 3:
            return 1.0

        # Mean reversion: if streak is UP, slight evidence for DOWN
        # Use market profile reversal rates
        asset_lower = asset.lower()
        try:
            mt = MarketType(f"{asset_lower}-updown-5m")
            profile = MARKET_PROFILES.get(mt, {})
        except ValueError:
            profile = {}

        reversal_key = f"streak_reversal_{min(streak, 6)}"
        reversal_rate = profile.get(reversal_key, 0.50 + streak * 0.02)

        w = self.accuracy["streak"].weight

        if last == "up":
            # Streak of UPs → lean DOWN (reversal)
            lr = (1 - reversal_rate) / reversal_rate * w
        else:
            # Streak of DOWNs → lean UP (reversal)
            lr = reversal_rate / (1 - reversal_rate) * w

        return max(0.4, min(2.5, lr))

    def _volatility_likelihood(self, regime: str) -> float:
        """Likelihood ratio from volatility regime.

        High vol slightly favors the current momentum direction (trends persist).
        Low vol is neutral. Extreme vol adds uncertainty.
        """
        # Volatility doesn't directly predict direction, but modulates confidence
        # We encode this as a slight bias toward momentum direction
        # The actual direction bias comes from momentum_likelihood
        if regime == "high":
            return 1.1  # slight momentum continuation bias
        elif regime == "extreme":
            return 1.0  # too noisy, no directional bias
        elif regime == "low":
            return 0.95  # slight mean-reversion bias
        return 1.0

    def update(
        self,
        asset: str,
        orderbook_imbalance: float = 0.0,
        vol_regime: str = "normal",
    ) -> float:
        """Recalculate posterior P(UP) using all evidence streams.

        Uses Bayesian update: posterior ∝ prior × Π(likelihood_ratios)
        """
        self._ensure_asset(asset)

        # Prior: base rate (starts at ~50%, may drift)
        try:
            mt = MarketType(f"{asset.lower()}-updown-5m")
            profile = MARKET_PROFILES.get(mt, {})
        except ValueError:
            profile = {}
        prior = profile.get("base_up_rate", 0.50)

        # Gather likelihood ratios
        lr_momentum = self._momentum_likelihood(asset)
        lr_volume = self._volume_imbalance_likelihood(asset)
        lr_orderbook = self._orderbook_likelihood(orderbook_imbalance)
        lr_streak = self._streak_likelihood(asset)
        lr_volatility = self._volatility_likelihood(vol_regime)

        # Bayesian update: P(UP|E) = P(E|UP)*P(UP) / [P(E|UP)*P(UP) + P(E|DOWN)*P(DOWN)]
        # With likelihood ratios: L = P(E|UP)/P(E|DOWN)
        # Combined L = product of all individual Ls
        combined_lr = lr_momentum * lr_volume * lr_orderbook * lr_streak * lr_volatility

        # Posterior odds = prior odds × combined LR
        prior_odds = prior / (1 - prior) if prior < 1.0 else 99.0
        posterior_odds = prior_odds * combined_lr
        posterior = posterior_odds / (1 + posterior_odds)

        # Clamp to [0.05, 0.95] to prevent overconfidence
        posterior = max(0.05, min(0.95, posterior))

        self._posteriors[asset] = posterior
        self._last_update[asset] = time.time()
        return posterior

    def get_probability(self, asset: str) -> float:
        """Get current P(UP) for an asset."""
        self._ensure_asset(asset)
        return self._posteriors.get(asset, 0.50)

    def get_confidence(self, asset: str) -> float:
        """Confidence = how far from 50%. Range 0-0.5."""
        p = self.get_probability(asset)
        return abs(p - 0.5)

    def get_direction(self, asset: str) -> str:
        """Predicted direction based on posterior."""
        return "up" if self.get_probability(asset) > 0.5 else "down"


# ═══════════════════════════════════════════════════════════════════════════════
# ORDERBOOK ANALYZER
# ═══════════════════════════════════════════════════════════════════════════════

class OrderbookAnalyzer:
    """Analyze Polymarket orderbook depth for trading intelligence.

    Provides:
      - Bid/ask imbalance (bullish/bearish pressure)
      - Slippage estimation for given trade sizes
      - Thin book detection (dangerous to trade)
      - Wall detection (large orders as support/resistance)
    """

    # Minimum depth to consider a book "thick" enough to trade
    MIN_DEPTH_USD = 20.0
    # Size threshold (in USD) for wall detection (relative to avg level)
    WALL_MULTIPLIER = 3.0

    def analyze(self, book: Optional["CachedOrderBook"]) -> OrderbookSnapshot:
        """Analyze a CachedOrderBook and return a snapshot."""
        if book is None or not book.bids or not book.asks:
            return OrderbookSnapshot(
                bid_depth_usd=0.0, ask_depth_usd=0.0, imbalance=0.0,
                spread=0.0, is_thin=True,
            )

        bid_depth = book.total_bid_depth
        ask_depth = book.total_ask_depth
        total = bid_depth + ask_depth

        imbalance = (bid_depth - ask_depth) / total if total > 0 else 0.0
        spread = book.spread
        is_thin = total < self.MIN_DEPTH_USD

        # Detect walls
        walls = self._detect_walls(book)

        # Slippage estimation
        slippage_10 = self._estimate_slippage(book, "BUY", 10.0)
        slippage_50 = self._estimate_slippage(book, "BUY", 50.0)

        return OrderbookSnapshot(
            bid_depth_usd=bid_depth,
            ask_depth_usd=ask_depth,
            imbalance=imbalance,
            spread=spread,
            is_thin=is_thin,
            walls=walls,
            slippage_10=slippage_10,
            slippage_50=slippage_50,
        )

    def _detect_walls(self, book: "CachedOrderBook") -> list[dict]:
        """Detect large orders that act as support/resistance."""
        walls = []

        for side_name, levels in [("bid", book.bids), ("ask", book.asks)]:
            if not levels:
                continue
            sizes = [lvl.value_usd for lvl in levels]
            if not sizes:
                continue
            avg_size = sum(sizes) / len(sizes)
            threshold = avg_size * self.WALL_MULTIPLIER

            for lvl in levels:
                if lvl.value_usd >= threshold and lvl.value_usd >= 10.0:
                    walls.append({
                        "side": side_name,
                        "price": lvl.price,
                        "size_usd": lvl.value_usd,
                    })

        return walls

    def _estimate_slippage(self, book: "CachedOrderBook", side: str, amount_usd: float) -> float:
        """Estimate slippage percentage for a given trade size."""
        exec_price, slippage_pct, _ = book.get_execution_price(side, amount_usd)
        return slippage_pct


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL AGGREGATOR
# ═══════════════════════════════════════════════════════════════════════════════

class SignalAggregator:
    """Combine all signals into consensus-based trade recommendations.

    Only emits a TradeSignal when multiple indicators agree.
    Tracks signal accuracy over time for self-learning weight adjustment.
    """

    # Minimum confidence to emit a signal
    MIN_CONFIDENCE = 0.10  # distance from 0.5
    # Minimum agreeing sub-signals
    MIN_CONSENSUS = 2

    def __init__(
        self,
        volatility_tracker: VolatilityTracker,
        bayesian_predictor: BayesianPredictor,
        orderbook_analyzer: OrderbookAnalyzer,
    ):
        self.vol = volatility_tracker
        self.bayes = bayesian_predictor
        self.ob = orderbook_analyzer

        # Signal history for accuracy tracking
        self._signal_history: deque = deque(maxlen=500)
        self._accuracy_log: list[dict] = []

    def evaluate(
        self,
        asset: str,
        market_type: MarketType,
        orderbook: Optional["CachedOrderBook"] = None,
    ) -> Optional[TradeSignal]:
        """Evaluate all signals and return a TradeSignal if consensus is reached."""

        # 1. Volatility
        vol_snap = self.vol.update(asset)
        regime = vol_snap.regime if vol_snap else "normal"

        # 2. Orderbook
        ob_snap = self.ob.analyze(orderbook)

        # 3. Bayesian posterior
        posterior = self.bayes.update(
            asset,
            orderbook_imbalance=ob_snap.imbalance,
            vol_regime=regime,
        )
        confidence = abs(posterior - 0.5)
        direction = "up" if posterior > 0.5 else "down"

        # 4. Count agreeing sub-signals
        signals_agreeing = 0
        reasons = []

        # Momentum agreement
        prices = self.bayes._price_history.get(asset)
        if prices and len(prices) > 5:
            p_now = prices[-1][1]
            cutoff = prices[-1][0] - 60
            p_old = None
            for ts, p in prices:
                if ts >= cutoff:
                    p_old = p
                    break
            if p_old and p_old > 0:
                mom = (p_now - p_old) / p_old * 100
                if (direction == "up" and mom > 0.02) or (direction == "down" and mom < -0.02):
                    signals_agreeing += 1
                    reasons.append(f"momentum {mom:+.3f}%")

        # Volume imbalance agreement
        buy_vol = self.bayes._buy_volume.get(asset)
        sell_vol = self.bayes._sell_volume.get(asset)
        if buy_vol or sell_vol:
            now = time.time()
            tb = sum(v for ts, v in (buy_vol or []) if now - ts < 120)
            ts_val = sum(v for ts, v in (sell_vol or []) if now - ts < 120)
            total = tb + ts_val
            if total > 50:
                vi = (tb - ts_val) / total
                if (direction == "up" and vi > 0.05) or (direction == "down" and vi < -0.05):
                    signals_agreeing += 1
                    reasons.append(f"vol_imbalance {vi:+.2f}")

        # Orderbook agreement
        if not ob_snap.is_thin:
            if (direction == "up" and ob_snap.imbalance > 0.1) or \
               (direction == "down" and ob_snap.imbalance < -0.1):
                signals_agreeing += 1
                reasons.append(f"ob_imbalance {ob_snap.imbalance:+.2f}")

        # Volatility regime supports strategy
        if regime in ("high", "extreme") and confidence > 0.15:
            signals_agreeing += 1
            reasons.append(f"vol_{regime}")

        # Streak agreement
        outcomes = self.bayes._outcomes.get(asset, [])
        if len(outcomes) >= 3:
            last = outcomes[-1]
            streak = 1
            for o in reversed(outcomes[:-1]):
                if o == last:
                    streak += 1
                else:
                    break
            if streak >= 3:
                reversal_dir = "down" if last == "up" else "up"
                if direction == reversal_dir:
                    signals_agreeing += 1
                    reasons.append(f"streak_reversal({streak})")

        # Check consensus
        if confidence < self.MIN_CONFIDENCE or signals_agreeing < self.MIN_CONSENSUS:
            return None

        # Thin book check
        if ob_snap.is_thin:
            return None

        # Size recommendation: scale with confidence and regime
        size_pct = min(1.0, confidence * 2)  # confidence 0.25 → 50% Kelly
        if regime == "high":
            size_pct *= 1.2  # more aggressive in high vol
        elif regime == "extreme":
            size_pct *= 0.5  # cautious in extreme vol
        elif regime == "low":
            size_pct *= 0.8  # less aggressive in low vol
        size_pct = min(1.0, max(0.1, size_pct))

        reason_str = f"{asset} {direction.upper()} P={posterior:.2f} [{', '.join(reasons)}]"

        signal = TradeSignal(
            asset=asset,
            direction=direction,
            confidence=confidence,
            bayesian_prob=posterior,
            volatility_regime=regime,
            recommended_size_pct=size_pct,
            reason=reason_str,
            orderbook_imbalance=ob_snap.imbalance,
            signal_count=signals_agreeing,
        )

        self._signal_history.append(signal)
        return signal

    def record_outcome(self, asset: str, outcome: str):
        """Record a market outcome for self-learning."""
        self.bayes.on_outcome(asset, outcome)

        # Check recent signals for this asset and log accuracy
        actual_up = outcome == "up"
        for sig in reversed(self._signal_history):
            if sig.asset == asset and time.time() - sig.timestamp < 600:
                predicted_up = sig.direction == "up"
                self._accuracy_log.append({
                    "asset": asset,
                    "predicted": sig.direction,
                    "actual": outcome,
                    "correct": predicted_up == actual_up,
                    "confidence": sig.confidence,
                    "timestamp": time.time(),
                })
                break

        # Trim log
        if len(self._accuracy_log) > 1000:
            self._accuracy_log = self._accuracy_log[-1000:]

    def get_accuracy_stats(self) -> dict:
        """Get accuracy statistics for self-learning review."""
        if not self._accuracy_log:
            return {"total": 0, "accuracy": 0.0}
        total = len(self._accuracy_log)
        correct = sum(1 for e in self._accuracy_log if e["correct"])
        return {
            "total": total,
            "accuracy": correct / total if total > 0 else 0.0,
            "sources": {
                name: {"accuracy": t.accuracy, "weight": t.weight, "n": t.predictions}
                for name, t in self.bayes.accuracy.items()
            },
        }


# ═══════════════════════════════════════════════════════════════════════════════
# BAYESIAN MODEL (SINGLETON FACADE)
# ═══════════════════════════════════════════════════════════════════════════════

class BayesianModel:
    """Top-level facade combining all components.

    Initialized once by the coordinator and shared across strategies.
    """

    def __init__(self):
        self.volatility = VolatilityTracker()
        self.predictor = BayesianPredictor()
        self.orderbook_analyzer = OrderbookAnalyzer()
        self.aggregator = SignalAggregator(
            self.volatility, self.predictor, self.orderbook_analyzer,
        )
        self._initialized = True
        print("[BAYESIAN] 🧠 Bayesian model initialized")

    # ── Price/Trade Feeds ─────────────────────────────────────────────────

    def on_price(self, asset: str, price: float, timestamp: Optional[float] = None):
        """Feed price tick from exchange."""
        self.volatility.on_price(asset, price, timestamp)

    def on_trade(self, asset: str, price: float, volume: float, is_buyer_maker: bool):
        """Feed trade from exchange. Updates both vol tracker and Bayesian predictor."""
        self.volatility.on_price(asset, price)
        self.predictor.on_trade(asset, price, volume, is_buyer_maker)

    def on_outcome(self, asset: str, outcome: str):
        """Feed market resolution outcome for self-learning."""
        self.aggregator.record_outcome(asset, outcome)

    # ── Integration API ───────────────────────────────────────────────────

    def get_directional_signal(
        self,
        asset: str,
        market_type: MarketType,
        orderbook: Optional["CachedOrderBook"] = None,
    ) -> Optional[TradeSignal]:
        """Get a consensus directional signal. Returns None if no strong signal."""
        return self.aggregator.evaluate(asset, market_type, orderbook)

    def get_volatility_regime(self, asset: str) -> str:
        """Get current volatility regime: 'low', 'normal', 'high', 'extreme'."""
        return self.volatility.get_regime(asset)

    def should_spread_farm(self, market_type) -> bool:
        """Whether spread farming is safe for this market's asset.

        True when volatility is low or normal (range-bound).
        False when extreme (too risky for both-side fills).
        """
        if market_type is None:
            return True  # Default to farming if no market specified
        asset = market_type.asset if hasattr(market_type, 'asset') else str(market_type)
        regime = self.get_volatility_regime(asset)
        return regime in ("low", "normal")

    def get_bayesian_probability(self, asset: str) -> float:
        """Get P(UP) for an asset. Range 0-1."""
        return self.predictor.get_probability(asset)

    def get_stats(self) -> dict:
        """Get model statistics for dashboard."""
        assets = list(self.predictor._posteriors.keys())
        return {
            "assets": {
                a: {
                    "p_up": self.predictor.get_probability(a),
                    "confidence": self.predictor.get_confidence(a),
                    "direction": self.predictor.get_direction(a),
                    "vol_regime": self.volatility.get_regime(a),
                    "vol_snapshot": {
                        "1m": self.volatility.get_snapshot(a).realized_vol_1m if self.volatility.get_snapshot(a) else None,
                        "5m": self.volatility.get_snapshot(a).realized_vol_5m if self.volatility.get_snapshot(a) else None,
                        "z_score": self.volatility.get_snapshot(a).z_score if self.volatility.get_snapshot(a) else None,
                    },
                }
                for a in assets
            },
            "accuracy": self.aggregator.get_accuracy_stats(),
            "signal_history_count": len(self.aggregator._signal_history),
        }
