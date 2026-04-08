"""Signal Detection Engine - Core algorithm for pump/dump detection."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class SignalScore:
    """Detailed signal score with all metrics."""

    symbol: str
    exchange: str
    score: float
    oi_change_pct: float
    price_change_pct: float
    volume_change_pct: float
    funding_rate: float
    long_short_ratio: float
    signal_type: str  # "PUMP" or "DUMP"
    confidence: str  # "LOW", "MEDIUM", "HIGH", "EXTREME"
    current_price: float = 0.0
    timeframe: str = "15m"
    stage: str = "CONFIRMED"  # "EARLY" or "CONFIRMED"
    details: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)

    @property
    def bias(self) -> str:
        return "LONG" if self.signal_type == "PUMP" else "SHORT"

    def to_message(self) -> str:
        """Format signal as Telegram message."""
        header_map = {
            ("PUMP", "EARLY"): ("🛰️", "РАННИЙ LONG WATCH"),
            ("DUMP", "EARLY"): ("🛰️", "РАННИЙ SHORT WATCH"),
            ("PUMP", "CONFIRMED"): ("🚀", "LONG СИГНАЛ"),
            ("DUMP", "CONFIRMED"): ("🔻", "SHORT СИГНАЛ"),
        }
        confidence_map = {
            "LOW": "⚪",
            "MEDIUM": "🟡",
            "HIGH": "🟠",
            "EXTREME": "🔴",
        }

        stage_emoji, title = header_map.get((self.signal_type, self.stage), ("📡", "SIGNAL"))
        confidence_emoji = confidence_map.get(self.confidence, "⚪")
        funding_emoji = "🟢" if self.funding_rate < 0 else "🔴"
        funding_text = "Шорты платят" if self.funding_rate < 0 else "Лонги платят"

        factor_translations = {
            "OI surge": "Всплеск OI",
            "OI rising": "Рост OI",
            "Price": "Цена",
            "Volume spike": "Всплеск объёма",
            "Negative funding": "Отрицательный фандинг",
            "Positive funding": "Положительный фандинг",
            "shorts pay": "шорты платят",
            "longs pay": "лонги платят",
            "short squeeze potential": "потенциал шорт-сквиза",
            "overcrowded longs": "перегруженные лонги",
            "bearish crowding": "перегруженные шорты",
            "bullish drift": "первые признаки роста",
            "bearish drift": "первые признаки снижения",
            "before expansion": "до импульса",
            "L/S ratio": "Соотношение L/S",
            "Volume build-up": "Нарастание объёма",
        }

        translated_factors = []
        for factor in self.details.get("factors", []):
            translated = factor
            for eng, rus in factor_translations.items():
                translated = translated.replace(eng, rus)
            translated_factors.append(translated)

        stage_text = (
            "Раннее предупреждение: давление набирается до расширения цены."
            if self.stage == "EARLY"
            else "Подтверждённый импульс по текущему таймфрейму."
        )

        message = (
            f"{stage_emoji} <b>{title}</b>\n\n"
            f"<b>Монета:</b> <code>{self.symbol}</code> ({self.exchange})\n"
            f"<b>Направление:</b> {self.bias}\n"
            f"<b>Таймфрейм:</b> {self.timeframe}\n"
            f"<b>Скор:</b> {self.score:.1f}/5 {confidence_emoji} {self.confidence}\n"
            f"<b>Статус:</b> {stage_text}\n\n"
            f"<b>Метрики окна:</b>\n"
            f"• OI: <code>{self.oi_change_pct:+.2f}%</code>\n"
            f"• Цена: <code>{self.price_change_pct:+.2f}%</code>\n"
            f"• Объём: <code>{self.volume_change_pct:+.1f}%</code>\n"
            f"• Фандинг: <code>{self.funding_rate * 100:.4f}%</code> {funding_emoji} ({funding_text})\n"
            f"• L/S: <code>{self.long_short_ratio:.2f}</code>\n"
            f"• Время: {self.timestamp.strftime('%H:%M:%S UTC')}\n"
        )

        if translated_factors:
            message += "\n<b>Факторы:</b>\n"
            for factor in translated_factors:
                message += f"• {factor}\n"

        return message


class SignalDetector:
    """Multi-factor pump/dump detection engine."""

    def __init__(
        self,
        oi_threshold: float = 5.0,
        price_threshold: float = 1.0,
        volume_threshold: float = 50.0,
        min_score: int = 3,
        lookback_minutes: int = 15,
    ):
        self.oi_threshold = oi_threshold
        self.price_threshold = price_threshold
        self.volume_threshold = volume_threshold
        self.min_score = min_score
        self.lookback_minutes = lookback_minutes

        self.history: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.last_signals: Dict[str, datetime] = {}

    def _history_key(self, exchange: str, symbol: str) -> str:
        return f"{exchange}:{symbol}"

    def update_history(self, key: str, data: Dict[str, Any], timestamp: Optional[datetime] = None) -> None:
        """Update historical data for one exchange/symbol pair."""
        now = timestamp or datetime.utcnow()

        self.history[key].append(
            {
                "timestamp": now,
                "oi": data.get("open_interest", 0.0),
                "price": data.get("price", 0.0),
                "volume": data.get("volume_24h", 0.0),
            }
        )

        cutoff = now.timestamp() - (self.lookback_minutes * 4 * 60)
        self.history[key] = [
            item
            for item in self.history[key]
            if item["timestamp"].timestamp() > cutoff
        ]

    def _get_baseline_point(self, key: str) -> Optional[Dict[str, Any]]:
        """Return the point closest to the configured lookback window."""
        points = self.history.get(key, [])
        if len(points) < 2:
            return None

        current_ts = points[-1]["timestamp"].timestamp()
        target_age = self.lookback_minutes * 60
        candidates = [
            point
            for point in points[:-1]
            if current_ts - point["timestamp"].timestamp() >= target_age
        ]
        if candidates:
            return candidates[-1]
        return None

    def _pct_change(self, current: float, previous: float) -> float:
        if previous == 0:
            return 0.0
        return ((current - previous) / previous) * 100

    def _calculate_window_volume_change(
        self,
        current_volume_24h: float,
        baseline_volume_24h: float,
    ) -> float:
        """Estimate recent volume anomaly vs normal 24h average flow."""
        window_volume = max(current_volume_24h - baseline_volume_24h, 0.0)
        expected_window_volume = max(current_volume_24h * (self.lookback_minutes / 1440.0), 1e-9)
        return ((window_volume - expected_window_volume) / expected_window_volume) * 100

    def _direction_from_pressure(
        self,
        price_change: float,
        funding_rate: float,
        long_short_ratio: float,
    ) -> str:
        """Resolve LONG/SHORT bias before price fully expands."""
        if abs(price_change) >= self.price_threshold * 0.35:
            return "PUMP" if price_change >= 0 else "DUMP"

        if funding_rate < 0 and long_short_ratio < 1.0:
            return "PUMP"
        if funding_rate > 0 and long_short_ratio > 1.15:
            return "DUMP"
        return "PUMP" if price_change >= 0 else "DUMP"

    def _cooldown_key(self, exchange: str, symbol: str, stage: str) -> str:
        return f"{exchange}:{symbol}:{stage}"

    def check_cooldown(self, exchange: str, symbol: str, stage: str) -> bool:
        """Check if signal stage is out of cooldown."""
        key = self._cooldown_key(exchange, symbol, stage)
        last_seen = self.last_signals.get(key)
        if not last_seen:
            return True

        cooldown = (
            settings.EARLY_SIGNAL_COOLDOWN_SECONDS
            if stage == "EARLY"
            else settings.SIGNAL_COOLDOWN_SECONDS
        )
        elapsed = (datetime.utcnow() - last_seen).total_seconds()
        return elapsed > cooldown

    def _register_signal(self, exchange: str, symbol: str, stage: str) -> None:
        self.last_signals[self._cooldown_key(exchange, symbol, stage)] = datetime.utcnow()

    def _score_confirmed_signal(
        self,
        symbol: str,
        exchange: str,
        signal_type: str,
        oi_change: float,
        price_change: float,
        volume_change: float,
        funding_rate: float,
        long_short_ratio: float,
        timestamp: datetime,
    ) -> Optional[SignalScore]:
        score = 0.0
        factors: List[str] = []

        if signal_type == "PUMP" and price_change < self.price_threshold:
            return None
        if signal_type == "DUMP" and price_change > -self.price_threshold:
            return None

        if signal_type == "PUMP":
            if oi_change >= self.oi_threshold:
                score += 1.0
                factors.append(f"OI surge +{oi_change:.1f}%")
            elif oi_change >= self.oi_threshold * 0.6:
                score += 0.5

            if price_change >= self.price_threshold:
                score += 1.0
                factors.append(f"Price +{price_change:.2f}%")

            if funding_rate < -0.0001:
                score += 1.0
                factors.append(f"Negative funding ({funding_rate * 100:.4f}%) - shorts pay")
            elif funding_rate > 0.001:
                score -= 0.5

            if long_short_ratio < 0.85:
                score += 1.0
                factors.append(f"L/S ratio {long_short_ratio:.2f} - short squeeze potential")
            elif long_short_ratio > 2.0:
                score -= 0.5
        else:
            if oi_change >= self.oi_threshold * 0.6:
                score += 1.0
                factors.append(f"OI rising +{oi_change:.1f}% during price drop")

            if price_change <= -self.price_threshold:
                score += 1.0
                factors.append(f"Price {price_change:.2f}%")

            if funding_rate > 0.0001:
                score += 1.0
                factors.append(f"Positive funding ({funding_rate * 100:.4f}%) - longs pay")
            elif funding_rate < -0.001:
                score -= 0.5

            if long_short_ratio > 1.8:
                score += 1.0
                factors.append(f"L/S ratio {long_short_ratio:.2f} - overcrowded longs")
            elif long_short_ratio < 0.5:
                score -= 0.5

        if volume_change >= self.volume_threshold:
            score += 1.0
            factors.append(f"Volume spike +{volume_change:.0f}%")
        elif volume_change >= self.volume_threshold * 0.5:
            score += 0.5

        if score < self.min_score:
            return None

        if not self.check_cooldown(exchange, symbol, "CONFIRMED"):
            return None

        self._register_signal(exchange, symbol, "CONFIRMED")
        return SignalScore(
            symbol=symbol,
            exchange=exchange,
            score=score,
            oi_change_pct=oi_change,
            price_change_pct=price_change,
            volume_change_pct=volume_change,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio if long_short_ratio > 0 else 1.0,
            signal_type=signal_type,
            confidence=self._confidence(score),
            current_price=current_price,
            stage="CONFIRMED",
            details={"factors": factors},
            timestamp=timestamp,
        )

    def _score_early_signal(
        self,
        symbol: str,
        exchange: str,
        signal_type: str,
        oi_change: float,
        price_change: float,
        volume_change: float,
        funding_rate: float,
        long_short_ratio: float,
        timestamp: datetime,
    ) -> Optional[SignalScore]:
        if not settings.ENABLE_EARLY_SIGNALS:
            return None

        score = 0.0
        factors: List[str] = []

        if oi_change >= self.oi_threshold * 0.6:
            score += 1.5
            factors.append(f"OI rising +{oi_change:.1f}% before expansion")
        elif oi_change >= self.oi_threshold * 0.4:
            score += 1.0

        if volume_change >= self.volume_threshold * 0.4:
            score += 1.0
            factors.append(f"Volume build-up +{volume_change:.0f}%")

        if signal_type == "PUMP":
            if funding_rate < -0.00005:
                score += 1.0
                factors.append(f"Negative funding ({funding_rate * 100:.4f}%) - shorts pay")
            if long_short_ratio < 0.95:
                score += 1.0
                factors.append(f"L/S ratio {long_short_ratio:.2f} - short squeeze potential")
            if 0 <= price_change < self.price_threshold:
                score += 0.5
                factors.append(f"Price +{price_change:.2f}% bullish drift")
            elif price_change < -self.price_threshold * 0.5:
                score -= 0.5
        else:
            if funding_rate > 0.00005:
                score += 1.0
                factors.append(f"Positive funding ({funding_rate * 100:.4f}%) - longs pay")
            if long_short_ratio > 1.4:
                score += 1.0
                factors.append(f"L/S ratio {long_short_ratio:.2f} - overcrowded longs")
            if 0 >= price_change > -self.price_threshold:
                score += 0.5
                factors.append(f"Price {price_change:.2f}% bearish drift")
            elif price_change > self.price_threshold * 0.5:
                score -= 0.5

        if score < settings.EARLY_SIGNAL_MIN_SCORE:
            return None

        if abs(price_change) >= self.price_threshold:
            return None

        if not self.check_cooldown(exchange, symbol, "EARLY"):
            return None

        self._register_signal(exchange, symbol, "EARLY")
        return SignalScore(
            symbol=symbol,
            exchange=exchange,
            score=score,
            oi_change_pct=oi_change,
            price_change_pct=price_change,
            volume_change_pct=volume_change,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio if long_short_ratio > 0 else 1.0,
            signal_type=signal_type,
            confidence=self._confidence(score),
            stage="EARLY",
            details={"factors": factors},
            timestamp=timestamp,
        )

    def _confidence(self, score: float) -> str:
        if score >= 5:
            return "EXTREME"
        if score >= 4:
            return "HIGH"
        if score >= 3:
            return "MEDIUM"
        return "LOW"

    async def process_market_data(
        self,
        exchange: str,
        data: Dict[str, Any],
        market_caps: Dict[str, float],
    ) -> List[SignalScore]:
        """Process batch of market data and generate signals."""
        signals: List[SignalScore] = []

        for symbol, market_data in data.items():
            try:
                base_symbol = symbol.replace("USDT", "").replace("USD", "")
                market_cap = market_caps.get(base_symbol, 0)
                if market_cap < settings.MIN_MARKET_CAP:
                    continue

                key = self._history_key(exchange, symbol)
                now = getattr(market_data, "timestamp", datetime.utcnow())
                current_snapshot = {
                    "open_interest": getattr(market_data, "open_interest", 0.0),
                    "price": getattr(market_data, "price", 0.0),
                    "volume_24h": getattr(market_data, "volume_24h", 0.0),
                }
                self.update_history(key, current_snapshot, timestamp=now)

                baseline = self._get_baseline_point(key)
                if not baseline:
                    continue

                current_price = current_snapshot["price"]
                current_oi = current_snapshot["open_interest"]
                current_volume = current_snapshot["volume_24h"]

                oi_change = self._pct_change(current_oi, baseline.get("oi", 0.0))
                price_change = self._pct_change(current_price, baseline.get("price", 0.0))
                volume_change = self._calculate_window_volume_change(
                    current_volume_24h=current_volume,
                    baseline_volume_24h=baseline.get("volume", 0.0),
                )
                funding_rate = getattr(market_data, "funding_rate", 0.0)
                long_short_ratio = getattr(market_data, "long_short_ratio", 1.0) or 1.0

                signal_type = self._direction_from_pressure(
                    price_change=price_change,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio,
                )

                confirmed_signal = self._score_confirmed_signal(
                    symbol=symbol,
                    exchange=exchange,
                    signal_type=signal_type,
                    oi_change=oi_change,
                    price_change=price_change,
                    volume_change=volume_change,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio,
                    timestamp=now,
                )
                if confirmed_signal:
                    signals.append(confirmed_signal)
                    logger.info(
                        "Confirmed signal detected: %s %s %.1f",
                        symbol,
                        confirmed_signal.bias,
                        confirmed_signal.score,
                    )
                    continue

                early_signal = self._score_early_signal(
                    symbol=symbol,
                    exchange=exchange,
                    signal_type=signal_type,
                    oi_change=oi_change,
                    price_change=price_change,
                    volume_change=volume_change,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio,
                    timestamp=now,
                )
                if early_signal:
                    signals.append(early_signal)
                    logger.info(
                        "Early signal detected: %s %s %.1f",
                        symbol,
                        early_signal.bias,
                        early_signal.score,
                    )
            except Exception as exc:
                logger.error("Error processing %s on %s: %s", symbol, exchange, exc)
                continue

        return signals
