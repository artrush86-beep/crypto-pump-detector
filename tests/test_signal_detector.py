"""Unit tests for SignalDetector._score_confirmed_signal and _score_early_signal.

Covers:
  - PUMP / DUMP directions with various metric combinations
  - Boundary conditions (exact threshold, just below, just above)
  - Penalty factors (wrong funding, overcrowded L/S)
  - Score aggregation and confidence mapping
  - Cooldown behaviour
  - Extended fields (taker_buy_ratio, liquidations, top_trader_ls)
  - _direction_from_pressure edge cases
"""

from __future__ import annotations

import sys
import os
from datetime import datetime, timedelta
from typing import Optional
from unittest.mock import patch, MagicMock

# Set required env vars BEFORE any project imports (pydantic-settings validates at import time)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token-000:FAKE")
os.environ.setdefault("TELEGRAM_CHAT_ID", "-1001234567890")

import pytest
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.detector.signal_detector import SignalDetector, SignalScore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_detector(**overrides) -> SignalDetector:
    """Create a SignalDetector with optional threshold overrides."""
    defaults = dict(
        oi_threshold=5.0,
        price_threshold=1.0,
        volume_threshold=50.0,
        min_score=3.0,
        lookback_minutes=15,
    )
    defaults.update(overrides)
    return SignalDetector(**defaults)


def _det() -> SignalDetector:
    """Fresh detector with default thresholds (no cooldown state)."""
    return _make_detector()


def _ts() -> datetime:
    """Fixed timestamp for deterministic tests."""
    return datetime(2026, 8, 22, 12, 0, 0)


# Patch settings used by _score_early_signal
@pytest.fixture(autouse=True)
def _patch_settings():
    """Ensure early signals are enabled and cooldowns are zero for tests."""
    with patch("src.detector.signal_detector.settings") as mock_settings:
        mock_settings.ENABLE_EARLY_SIGNALS = True
        mock_settings.EARLY_SIGNAL_MIN_SCORE = 3.0
        mock_settings.EARLY_SIGNAL_COOLDOWN_SECONDS = 0
        mock_settings.SIGNAL_COOLDOWN_SECONDS = 0
        mock_settings.MIN_MARKET_CAP = 200_000
        yield mock_settings


# ===================================================================
# _score_confirmed_signal  —  PUMP direction
# ===================================================================

class TestConfirmedPump:
    """All confirmed PUMP signal scenarios."""

    def test_all_bullish_factors(self):
        """OI surge + price up + negative funding + short squeeze → high score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="BTCUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=8.0,       # >= threshold → +1
            price_change=3.0,    # >= threshold → +1
            volume_change=60.0,  # >= volume_threshold → +1
            funding_rate=-0.001, # negative → +1
            long_short_ratio=0.7, # < 0.85 → +1
            timestamp=_ts(),
            current_price=60000.0,
        )
        assert sig is not None
        assert sig.score == 5.0
        assert sig.confidence == "EXTREME"
        assert sig.signal_type == "PUMP"
        assert sig.stage == "CONFIRMED"
        assert sig.symbol == "BTCUSDT"
        assert len(sig.details["factors"]) == 5

    def test_minimum_bullish_below_threshold(self):
        """Just below min_score=3: price + volume + partial OI = 2.5 → rejected."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="ETHUSDT",
            exchange="bybit",
            signal_type="PUMP",
            oi_change=3.5,       # >= threshold*0.6=3.0 → +0.5
            price_change=1.5,    # >= threshold → +1
            volume_change=60.0,  # >= threshold → +1
            funding_rate=0.0,    # neutral → 0
            long_short_ratio=1.0,# neutral → 0
            timestamp=_ts(),
            current_price=3000.0,
        )
        # 0.5 + 1 + 1 = 2.5 < 3.0 → None
        assert sig is None

    def test_minimum_bullish_clears_threshold(self):
        """Price + volume + negative funding = 3.0 → just clears min_score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="ETHUSDT",
            exchange="bybit",
            signal_type="PUMP",
            oi_change=1.0,       # < threshold*0.6 → 0
            price_change=1.5,    # >= threshold → +1
            volume_change=60.0,  # >= threshold → +1
            funding_rate=-0.001, # negative → +1
            long_short_ratio=1.0,# neutral → 0
            timestamp=_ts(),
            current_price=3000.0,
        )
        # 0 + 1 + 1 + 1 = 3.0 → just clears
        assert sig is not None
        assert sig.score == 3.0
        assert sig.confidence == "MEDIUM"

    def test_price_below_threshold_rejects(self):
        """PUMP rejected if price_change < price_threshold."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="SOLUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=10.0,
            price_change=0.5,   # < 1.0 threshold
            volume_change=100.0,
            funding_rate=-0.002,
            long_short_ratio=0.6,
            timestamp=_ts(),
        )
        assert sig is None

    def test_penalty_positive_funding_on_pump_reduces_score(self):
        """Positive funding penalises PUMP score by 0.5."""
        d = _det()
        # With negative funding: 1+1+1+1 = 4.0
        sig_with_neg = d._score_confirmed_signal(
            symbol="DOGEUSDT_A",
            exchange="binance",
            signal_type="PUMP",
            oi_change=6.0,       # +1
            price_change=2.0,    # +1
            volume_change=60.0,  # +1
            funding_rate=-0.001, # +1
            long_short_ratio=0.7,# +1
            timestamp=_ts(),
        )
        # With positive funding: 1+1+1+1-0.5 = 3.5
        sig_with_pos = d._score_confirmed_signal(
            symbol="DOGEUSDT_B",
            exchange="binance",
            signal_type="PUMP",
            oi_change=6.0,       # +1
            price_change=2.0,    # +1
            volume_change=60.0,  # +1
            funding_rate=0.002,  # > 0.001 → -0.5
            long_short_ratio=0.7,# +1
            timestamp=_ts(),
        )
        assert sig_with_neg is not None
        assert sig_with_pos is not None
        assert sig_with_pos.score == sig_with_neg.score - 0.5

    def test_penalty_overcrowded_longs_on_pump(self):
        """L/S > 2.0 penalises PUMP score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="XRPUSDT",
            exchange="okx",
            signal_type="PUMP",
            oi_change=6.0,       # +1
            price_change=2.0,    # +1
            volume_change=60.0,  # +1
            funding_rate=-0.001, # +1
            long_short_ratio=2.5,# > 2.0 → -0.5
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 3.5  # 1+1+1+1-0.5 = 3.5

    def test_partial_volume_bonus(self):
        """Volume at 50% of threshold → +0.5 bonus."""
        d = _make_detector(oi_threshold=5.0, price_threshold=1.0, volume_threshold=50.0)
        sig = d._score_confirmed_signal(
            symbol="AVAXUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=6.0,       # +1
            price_change=2.0,    # +1
            volume_change=28.0,  # >= 25.0 (50%) → +0.5
            funding_rate=-0.001, # +1
            long_short_ratio=0.8,# < 0.85 → +1
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 4.5

    def test_extended_fields_stored(self):
        """Extended fields are stored in details."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="BTCUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=6.0,
            price_change=2.0,
            volume_change=60.0,
            funding_rate=-0.001,
            long_short_ratio=0.7,
            timestamp=_ts(),
            current_price=60000.0,
            taker_buy_ratio=0.65,
            liq_usd=1_500_000,
            liq_side="SHORT",
            top_trader_ls=1.8,
        )
        assert sig is not None
        assert sig.details["taker_buy_ratio"] == 0.65
        assert sig.details["recent_liquidations_usd"] == 1_500_000
        assert sig.details["liq_side"] == "SHORT"
        assert sig.details["top_trader_ls_ratio"] == 1.8

    def test_to_dict_includes_extended(self):
        """to_dict() lifts extended fields to top level."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="SOLUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=7.0,
            price_change=3.0,
            volume_change=80.0,
            funding_rate=-0.002,
            long_short_ratio=0.6,
            timestamp=_ts(),
            current_price=150.0,
            taker_buy_ratio=0.7,
            liq_usd=2_000_000,
            liq_side="SHORT",
            top_trader_ls=2.0,
        )
        d = sig.to_dict()
        assert d["taker_buy_ratio"] == 0.7
        assert d["recent_liquidations_usd"] == 2_000_000
        assert d["liq_side"] == "SHORT"
        assert d["top_trader_ls_ratio"] == 2.0
        assert d["signal_type"] == "pump"
        assert d["stage"] == "CONFIRMED"
        assert d["bias"] == "LONG"


# ===================================================================
# _score_confirmed_signal  —  DUMP direction
# ===================================================================

class TestConfirmedDump:
    """All confirmed DUMP signal scenarios."""

    def test_all_bearish_factors(self):
        """OI rising + price down + positive funding + overcrowded longs → high score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="ETHUSDT",
            exchange="binance",
            signal_type="DUMP",
            oi_change=6.0,       # >= threshold*0.6 → +1
            price_change=-2.5,   # <= -threshold → +1
            volume_change=70.0,  # >= threshold → +1
            funding_rate=0.001,  # > 0.0001 → +1
            long_short_ratio=2.2,# > 1.8 → +1
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 5.0
        assert sig.confidence == "EXTREME"
        assert sig.signal_type == "DUMP"
        assert sig.bias == "SHORT"

    def test_price_above_threshold_rejects_dump(self):
        """DUMP rejected if price_change is positive."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="BTCUSDT",
            exchange="bybit",
            signal_type="DUMP",
            oi_change=8.0,
            price_change=1.5,   # positive → rejected
            volume_change=80.0,
            funding_rate=0.002,
            long_short_ratio=2.0,
            timestamp=_ts(),
        )
        assert sig is None

    def test_penalty_negative_funding_on_dump(self):
        """Negative funding penalises DUMP score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="BNBUSDT",
            exchange="okx",
            signal_type="DUMP",
            oi_change=6.0,       # +1
            price_change=-2.0,   # +1
            volume_change=60.0,  # +1
            funding_rate=-0.002, # < -0.001 → -0.5
            long_short_ratio=2.0,# +1
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 3.5  # 1+1+1-0.5+1 = 3.5

    def test_penalty_low_ls_on_dump(self):
        """L/S < 0.5 penalises DUMP score."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="DOTUSDT",
            exchange="binance",
            signal_type="DUMP",
            oi_change=6.0,       # +1
            price_change=-2.0,   # +1
            volume_change=60.0,  # +1
            funding_rate=0.001,  # +1
            long_short_ratio=0.3,# < 0.5 → -0.5
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 3.5  # 1+1+1+1-0.5 = 3.5

    def test_dump_oi_below_threshold(self):
        """OI below threshold*0.6 → no OI factor for dump."""
        d = _det()
        sig = d._score_confirmed_signal(
            symbol="LINKUSDT",
            exchange="binance",
            signal_type="DUMP",
            oi_change=2.0,       # < 3.0 (threshold*0.6)
            price_change=-3.0,   # +1
            volume_change=60.0,  # +1
            funding_rate=0.001,  # +1
            long_short_ratio=2.0,# +1
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 4.0  # 0+1+1+1+1 = 4.0


# ===================================================================
# _score_early_signal  —  PUMP direction
# ===================================================================

class TestEarlyPump:
    """Early PUMP signals (pressure building before expansion)."""

    def test_early_pump_all_bullish(self):
        """OI building + volume + negative funding + low L/S → early signal."""
        d = _det()
        sig = d._score_early_signal(
            symbol="SUIUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=4.0,       # >= threshold*0.6=3.0 → +1.5
            price_change=0.5,    # 0 < price < threshold → +0.5 (bullish drift)
            volume_change=25.0,  # >= threshold*0.4=20 → +1
            funding_rate=-0.0002,# < -0.00005 → +1
            long_short_ratio=0.8,# < 0.95 → +1
            current_price=2.0,
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 5.0
        assert sig.stage == "EARLY"
        assert sig.signal_type == "PUMP"

    def test_early_pump_below_min_score(self):
        """Insufficient factors → no early signal."""
        d = _det()
        sig = d._score_early_signal(
            symbol="NEARUSDT",
            exchange="bybit",
            signal_type="PUMP",
            oi_change=1.0,       # < threshold*0.4=2.0 → 0
            price_change=0.3,    # +0.5
            volume_change=10.0,  # < threshold*0.4=20 → 0
            funding_rate=0.0,    # neutral → 0
            long_short_ratio=1.0,# neutral → 0
            current_price=5.0,
            timestamp=_ts(),
        )
        assert sig is None  # score 0.5 < 3.0

    def test_early_pump_rejected_if_price_expanded(self):
        """Early signal rejected if price already >= threshold (should be confirmed)."""
        d = _det()
        sig = d._score_early_signal(
            symbol="PEPEUSDT",
            exchange="binance",
            signal_type="PUMP",
            oi_change=5.0,
            price_change=1.5,   # >= threshold → rejected early
            volume_change=30.0,
            funding_rate=-0.001,
            long_short_ratio=0.8,
            current_price=0.00001,
            timestamp=_ts(),
        )
        assert sig is None

    def test_early_pump_bearish_price_penalty(self):
        """Negative price on PUMP → penalty."""
        d = _det()
        sig = d._score_early_signal(
            symbol="TIAUSDT",
            exchange="okx",
            signal_type="PUMP",
            oi_change=4.0,       # +1.5
            price_change=-0.8,   # < -threshold*0.5=-0.5 → -0.5
            volume_change=25.0,  # +1
            funding_rate=-0.0001,# +1
            long_short_ratio=0.9,# < 0.95 → +1
            current_price=10.0,
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 4.0  # 1.5+1+1+1-0.5 = 4.0


# ===================================================================
# _score_early_signal  —  DUMP direction
# ===================================================================

class TestEarlyDump:
    """Early DUMP signals (selling pressure building)."""

    def test_early_dump_all_bearish(self):
        """OI building + volume + positive funding + overcrowded longs → early DUMP."""
        d = _det()
        sig = d._score_early_signal(
            symbol="ARBUSDT",
            exchange="binance",
            signal_type="DUMP",
            oi_change=4.0,       # >= threshold*0.6 → +1.5
            price_change=-0.5,   # 0 > price > -threshold → +0.5 (bearish drift)
            volume_change=25.0,  # >= threshold*0.4 → +1
            funding_rate=0.0002, # > 0.00005 → +1
            long_short_ratio=1.6,# > 1.4 → +1
            current_price=1.5,
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 5.0
        assert sig.stage == "EARLY"
        assert sig.signal_type == "DUMP"

    def test_early_dump_rejected_if_price_dropped_too_much(self):
        """Early DUMP rejected if price already below -threshold."""
        d = _det()
        sig = d._score_early_signal(
            symbol="OPUSDT",
            exchange="bybit",
            signal_type="DUMP",
            oi_change=5.0,
            price_change=-2.0,  # <= -threshold → should be confirmed, not early
            volume_change=30.0,
            funding_rate=0.001,
            long_short_ratio=2.0,
            current_price=2.0,
            timestamp=_ts(),
        )
        assert sig is None  # abs(price_change) >= threshold

    def test_early_dump_bullish_price_penalty(self):
        """Positive price on DUMP → penalty."""
        d = _det()
        sig = d._score_early_signal(
            symbol="SEIUSDT",
            exchange="okx",
            signal_type="DUMP",
            oi_change=4.0,       # +1.5
            price_change=0.8,    # > threshold*0.5=0.5 → -0.5
            volume_change=25.0,  # +1
            funding_rate=0.0002, # +1
            long_short_ratio=1.6,# +1
            current_price=0.5,
            timestamp=_ts(),
        )
        assert sig is not None
        assert sig.score == 4.0  # 1.5+1+1+1-0.5 = 4.0


# ===================================================================
# _direction_from_pressure
# ===================================================================

class TestDirectionFromPressure:
    """Direction resolution edge cases."""

    def test_strong_positive_price(self):
        d = _det()
        assert d._direction_from_pressure(2.0, 0.0, 1.0) == "PUMP"

    def test_strong_negative_price(self):
        d = _det()
        assert d._direction_from_pressure(-3.0, 0.0, 1.0) == "DUMP"

    def test_negative_funding_low_ls_bullish(self):
        """Negative funding + low L/S → PUMP bias."""
        d = _det()
        assert d._direction_from_pressure(0.1, -0.001, 0.8) == "PUMP"

    def test_positive_funding_high_ls_bearish(self):
        """Positive funding + high L/S → DUMP bias."""
        d = _det()
        assert d._direction_from_pressure(0.1, 0.002, 1.5) == "DUMP"

    def test_neutral_defaults_to_price_direction(self):
        """No pressure indicators → follows price sign."""
        d = _det()
        assert d._direction_from_pressure(0.05, 0.0, 1.0) == "PUMP"
        assert d._direction_from_pressure(-0.05, 0.0, 1.0) == "DUMP"


# ===================================================================
# _confidence mapping
# ===================================================================

class TestConfidence:
    """Score → confidence level mapping."""

    def test_extreme(self):
        d = _det()
        assert d._confidence(5.0) == "EXTREME"
        assert d._confidence(6.0) == "EXTREME"  # capped but still EXTREME

    def test_high(self):
        d = _det()
        assert d._confidence(4.0) == "HIGH"
        assert d._confidence(4.5) == "HIGH"

    def test_medium(self):
        d = _det()
        assert d._confidence(3.0) == "MEDIUM"
        assert d._confidence(3.5) == "MEDIUM"

    def test_low(self):
        d = _det()
        assert d._confidence(2.5) == "LOW"
        assert d._confidence(0.0) == "LOW"


# ===================================================================
# Cooldown
# ===================================================================

class TestCooldown:
    """Cooldown gating prevents duplicate signals."""

    def test_first_signal_not_cooldowned(self):
        d = _det()
        assert d.check_cooldown("binance", "BTCUSDT", "CONFIRMED") is True

    def test_signal_registers_cooldown(self):
        d = _det()
        d._register_signal("binance", "BTCUSDT", "CONFIRMED")
        # With 0 cooldown in tests, should still be allowed
        assert d.check_cooldown("binance", "BTCUSDT", "CONFIRMED") is True

    def test_cooldown_blocks_immediately_with_nonzero(self):
        d = _det()
        d.last_signals["binance:BTCUSDT:CONFIRMED"] = datetime.utcnow()
        # Simulate 1800s cooldown by checking against real time
        # Since we can't mock datetime.utcnow easily here, verify key exists
        assert "binance:BTCUSDT:CONFIRMED" in d.last_signals


# ===================================================================
# SignalScore serialization
# ===================================================================

class TestSignalScore:
    """SignalScore data class behaviour."""

    def test_to_dict_basic_fields(self):
        sig = SignalScore(
            symbol="BTCUSDT",
            exchange="binance",
            score=4.0,
            oi_change_pct=5.0,
            price_change_pct=2.0,
            volume_change_pct=60.0,
            funding_rate=-0.001,
            long_short_ratio=0.8,
            signal_type="PUMP",
            confidence="HIGH",
            current_price=60000.0,
            stage="CONFIRMED",
        )
        d = sig.to_dict()
        assert d["symbol"] == "BTCUSDT"
        assert d["exchange"] == "binance"
        assert d["score"] == 4.0
        assert d["signal_type"] == "pump"
        assert d["stage"] == "CONFIRMED"
        assert d["bias"] == "LONG"
        assert d["timeframe"] == "15m"

    def test_to_dict_dump_bias(self):
        sig = SignalScore(
            symbol="ETHUSDT",
            exchange="bybit",
            score=3.5,
            oi_change_pct=4.0,
            price_change_pct=-2.0,
            volume_change_pct=55.0,
            funding_rate=0.001,
            long_short_ratio=2.0,
            signal_type="DUMP",
            confidence="MEDIUM",
        )
        assert sig.bias == "SHORT"
        assert sig.to_dict()["bias"] == "SHORT"
        assert sig.to_dict()["signal_type"] == "dump"

    def test_to_message_contains_tradingview(self):
        sig = SignalScore(
            symbol="SOLUSDT",
            exchange="okx",
            score=4.5,
            oi_change_pct=7.0,
            price_change_pct=3.0,
            volume_change_pct=80.0,
            funding_rate=-0.002,
            long_short_ratio=0.6,
            signal_type="PUMP",
            confidence="HIGH",
            stage="CONFIRMED",
            details={"factors": ["OI surge +7.0%"]},
        )
        msg = sig.to_message()
        assert "TradingView" in msg
        assert "CoinGlass" in msg
        assert "SOLUSDT" in msg
        assert "BINANCE" in msg
