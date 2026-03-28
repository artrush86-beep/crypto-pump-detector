"""Signal Detection Engine - Core algorithm for pump/dump detection."""

import logging
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime
from collections import defaultdict
import asyncio

logger = logging.getLogger(__name__)


@dataclass
class SignalScore:
    """Detailed signal score with all metrics."""
    symbol: str
    exchange: str
    score: int  # 0-5
    oi_change_pct: float
    price_change_pct: float
    volume_change_pct: float
    funding_rate: float
    long_short_ratio: float
    signal_type: str  # "PUMP" or "DUMP"
    confidence: str  # "LOW", "MEDIUM", "HIGH", "EXTREME"
    timeframe: str = "15m"  # "5m", "15m", "1h"
    details: Dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.utcnow)
    
    def to_message(self) -> str:
        """Format signal as Telegram message."""
        emoji = {
            "PUMP": "�",  # Green circle for pump
            "DUMP": "�",  # Red circle for dump
            "LOW": "⚪",
            "MEDIUM": "🟡",
            "HIGH": "🟠",
            "EXTREME": "🔴"
        }
        
        # Signal direction emoji
        direction = "📈" if self.signal_type == "PUMP" else "📉"
        signal_emoji = emoji.get(self.signal_type, "⚪")
        
        # Funding interpretation
        funding_emoji = "🟢" if self.funding_rate < 0 else "🔴"
        funding_text = "Шорты платят" if self.funding_rate < 0 else "Лонги платят"
        
        # Translate factors to Russian
        factor_translations = {
            "OI surge": "Всплеск OI",
            "OI rising": "Рост OI",
            "Price": "Цена",
            "Volume spike": "Всплеск объёма",
            "Negative funding": "Отрицательный фандинг",
            "Positive funding": "Положительный фандинг",
            "shorts pay": "шорты платят",
            "longs pay": "лонги платят",
            "short squeeze potential": "потенциал шорт скуиз",
            "overcrowded longs": "перегруженные лонги",
            "during price drop": "во время падения цены",
            "L/S ratio": "Соотношение L/S"
        }
        
        # Translate factors
        translated_factors = []
        for factor in self.details.get("factors", []):
            translated = factor
            for eng, rus in factor_translations.items():
                translated = translated.replace(eng, rus)
            translated_factors.append(translated)
        
        message = f"""
{signal_emoji} <b>{self.signal_type} СИГНАЛ</b> {signal_emoji}

<b>Монета:</b> <code>{self.symbol}</code> ({self.exchange})
<b>Таймфрейм:</b> {self.timeframe}
<b>Скор:</b> {self.score}/5 {emoji.get(self.confidence, '')} {self.confidence}

<b>Метрики:</b>
• OI: <code>{self.oi_change_pct:+.2f}%</code> {'✅' if abs(self.oi_change_pct) >= 5 else '⚠️'}
• Цена: <code>{self.price_change_pct:+.2f}%</code> {'✅' if abs(self.price_change_pct) >= 1 else '⚠️'}
• Объём: <code>{self.volume_change_pct:+.1f}%</code> {'✅' if self.volume_change_pct >= 50 else '⚠️'}
• Фандинг: <code>{self.funding_rate*100:.4f}%</code> {funding_emoji} ({funding_text})
• L/S: <code>{self.long_short_ratio:.2f}</code> {'✅' if (self.signal_type == 'PUMP' and self.long_short_ratio < 1) or (self.signal_type == 'DUMP' and self.long_short_ratio > 2) else '⚠️'}

<b>Время:</b> {self.timestamp.strftime('%H:%M:%S UTC')}
"""
        
        # Add translated details
        if translated_factors:
            message += f"\n<b>Факторы:</b>\n"
            for factor in translated_factors:
                message += f"• {factor}\n"
        
        return message


class SignalDetector:
    """Multi-factor pump/dump detection engine."""
    
    def __init__(
        self,
        oi_threshold: float = 5.0,  # 5% OI change
        price_threshold: float = 1.0,  # 1% price change
        volume_threshold: float = 50.0,  # 50% volume spike
        min_score: int = 3,
        lookback_minutes: int = 15
    ):
        self.oi_threshold = oi_threshold
        self.price_threshold = price_threshold
        self.volume_threshold = volume_threshold
        self.min_score = min_score
        self.lookback_minutes = lookback_minutes
        
        # Historical data storage for calculating changes
        self.history = defaultdict(list)  # symbol -> list of data points
        self.last_signals = {}  # symbol -> timestamp (anti-spam)
        self.cooldown_seconds = 1800  # 30 min between signals for same symbol
    
    def calculate_oi_change(self, current_oi: float, symbol: str) -> float:
        """Calculate OI change from history."""
        if symbol not in self.history or len(self.history[symbol]) < 2:
            return 0.0
        
        # Get OI from ~15 minutes ago
        old_point = self.history[symbol][0]
        old_oi = old_point.get('oi', 0)
        
        if old_oi > 0:
            return ((current_oi - old_oi) / old_oi) * 100
        return 0.0
    
    def calculate_volume_change(self, current_volume: float, avg_volume: float) -> float:
        """Calculate volume change vs average."""
        if avg_volume > 0:
            return ((current_volume - avg_volume) / avg_volume) * 100
        return 0.0
    
    def update_history(self, symbol: str, data: Dict):
        """Update historical data for symbol."""
        now = datetime.utcnow()
        
        # Add new point
        self.history[symbol].append({
            'timestamp': now,
            'oi': data.get('open_interest', 0),
            'price': data.get('price', 0),
            'volume': data.get('volume_24h', 0)
        })
        
        # Keep only last 30 minutes of data (2x lookback)
        cutoff = now.timestamp() - (self.lookback_minutes * 2 * 60)
        self.history[symbol] = [
            h for h in self.history[symbol]
            if h['timestamp'].timestamp() > cutoff
        ]
    
    def check_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in cooldown period."""
        if symbol not in self.last_signals:
            return True
        
        elapsed = (datetime.utcnow() - self.last_signals[symbol]).total_seconds()
        return elapsed > self.cooldown_seconds
    
    def score_signal(
        self,
        symbol: str,
        exchange: str,
        oi_change: float,
        price_change: float,
        volume_change: float,
        funding_rate: float,
        long_short_ratio: float,
        market_data: Dict
    ) -> Optional[SignalScore]:
        """Calculate signal score and return if significant."""
        
        score = 0
        factors = []
        signal_type = "PUMP"
        
        # Determine signal type based on price direction
        if price_change < 0:
            signal_type = "DUMP"
        
        # Factor 1: Open Interest Change
        oi_significant = False
        if signal_type == "PUMP":
            if oi_change >= self.oi_threshold:
                score += 1
                factors.append(f"OI surge +{oi_change:.1f}%")
                oi_significant = True
            elif oi_change >= self.oi_threshold * 0.5:
                score += 0.5
        else:  # DUMP
            if oi_change >= self.oi_threshold * 0.5:  # OI rising during dump = potential short squeeze later or distribution
                score += 1
                factors.append(f"OI rising +{oi_change:.1f}% during price drop")
                oi_significant = True
        
        # Factor 2: Price Change
        price_significant = abs(price_change) >= self.price_threshold
        if price_significant:
            score += 1
            factors.append(f"Price {'+' if price_change > 0 else ''}{price_change:.2f}%")
        
        # Factor 3: Volume Spike
        volume_significant = volume_change >= self.volume_threshold
        if volume_significant:
            score += 1
            factors.append(f"Volume spike +{volume_change:.0f}%")
        
        # Factor 4: Funding Rate (contrarian indicator)
        # Negative funding + rising OI = potential pump (shorts trapped)
        # High positive funding + rising OI = potential dump (longs trapped)
        if signal_type == "PUMP":
            if funding_rate < -0.0001:  # Negative funding
                score += 1
                factors.append(f"Negative funding ({funding_rate*100:.4f}%) - shorts pay")
            elif funding_rate > 0.001:  # Very high positive funding
                score -= 0.5  # Penalty - overleveraged longs
        else:  # DUMP
            if funding_rate > 0.0001:  # Positive funding
                score += 1
                factors.append(f"Positive funding ({funding_rate*100:.4f}%) - longs pay")
            elif funding_rate < -0.001:  # Very negative funding
                score -= 0.5  # Penalty - overleveraged shorts
        
        # Factor 5: Long/Short Ratio
        if long_short_ratio > 0:
            if signal_type == "PUMP":
                if long_short_ratio < 0.8:  # More shorts than longs
                    score += 1
                    factors.append(f"L/S ratio {long_short_ratio:.2f} - short squeeze potential")
                elif long_short_ratio > 2.0:  # Too many longs
                    score -= 0.5  # Overcrowded long trade
            else:  # DUMP
                if long_short_ratio > 2.0:  # More longs than shorts
                    score += 1
                    factors.append(f"L/S ratio {long_short_ratio:.2f} - overcrowded longs")
                elif long_short_ratio < 0.5:  # Too many shorts
                    score -= 0.5
        
        # Check minimum score
        if score < self.min_score:
            return None
        
        # Check cooldown
        if not self.check_cooldown(symbol):
            return None
        
        # Determine confidence
        if score >= 5:
            confidence = "EXTREME"
        elif score >= 4:
            confidence = "HIGH"
        elif score >= 3:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        
        # Record signal time
        self.last_signals[symbol] = datetime.utcnow()
        
        return SignalScore(
            symbol=symbol,
            exchange=exchange,
            score=int(score),
            oi_change_pct=oi_change,
            price_change_pct=price_change,
            volume_change_pct=volume_change,
            funding_rate=funding_rate,
            long_short_ratio=long_short_ratio if long_short_ratio > 0 else 1.0,
            signal_type=signal_type,
            confidence=confidence,
            details={"factors": factors}
        )
    
    async def process_market_data(
        self,
        exchange: str,
        data: Dict[str, Any],
        market_caps: Dict[str, float]
    ) -> List[SignalScore]:
        """Process batch of market data and generate signals."""
        signals = []
        
        for symbol, market_data in data.items():
            try:
                # Filter by market cap
                base_symbol = symbol.replace("USDT", "").replace("USD", "")
                market_cap = market_caps.get(base_symbol, 0)
                
                if market_cap < 10_000_000:  # $10M minimum
                    continue
                
                # Update history
                self.update_history(symbol, {
                    'open_interest': getattr(market_data, 'open_interest', 0),
                    'price': getattr(market_data, 'price', 0),
                    'volume_24h': getattr(market_data, 'volume_24h', 0)
                })
                
                # Need history to calculate changes
                if len(self.history[symbol]) < 2:
                    continue
                
                # Calculate metrics
                current = self.history[symbol][-1]
                previous = self.history[symbol][0]
                
                oi_change = self.calculate_oi_change(
                    getattr(market_data, 'open_interest', 0),
                    symbol
                )
                
                # Use 24h price change as proxy for recent change
                price_change = getattr(market_data, 'price_change_24h', 0)
                
                # Estimate volume change (compare to average)
                current_volume = getattr(market_data, 'volume_24h', 0)
                avg_volume = previous.get('volume', current_volume)
                volume_change = self.calculate_volume_change(current_volume, avg_volume)
                
                funding_rate = getattr(market_data, 'funding_rate', 0)
                long_short_ratio = getattr(market_data, 'long_short_ratio', 1.0)
                
                # Score the signal
                signal = self.score_signal(
                    symbol=symbol,
                    exchange=exchange,
                    oi_change=oi_change,
                    price_change=price_change,
                    volume_change=volume_change,
                    funding_rate=funding_rate,
                    long_short_ratio=long_short_ratio if long_short_ratio else 1.0,
                    market_data={}
                )
                
                if signal:
                    signals.append(signal)
                    logger.info(f"Signal detected: {symbol} {signal.signal_type} Score: {signal.score}")
                
            except Exception as e:
                logger.error(f"Error processing {symbol}: {e}")
                continue
        
        return signals
