"""Redis client for signal storage (Upstash compatible)."""

import json
import logging
from typing import List, Dict, Optional
from datetime import datetime
import redis.asyncio as redis
from redis.asyncio import Redis

from config.settings import settings

logger = logging.getLogger(__name__)


class RedisSignalsStore:
    """Redis-based signal storage for persistent data across restarts."""
    
    def __init__(self):
        self.redis: Optional[Redis] = None
        self._connected = False
        
    async def connect(self):
        """Initialize Redis connection."""
        if not settings.REDIS_URL:
            logger.info("Redis URL not configured, skipping Redis connection")
            return False
            
        try:
            self.redis = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_keepalive=True,
                health_check_interval=30,
            )
            await self.redis.ping()
            self._connected = True
            logger.info("Connected to Redis (Upstash)")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            self.redis = None
            return False
    
    async def save_signal(self, signal_data: Dict) -> bool:
        """Save signal to Redis with TTL."""
        if not self._connected or not self.redis:
            return False
            
        try:
            # Generate unique key with timestamp
            timestamp = signal_data.get('timestamp', datetime.utcnow().isoformat())
            symbol = signal_data.get('symbol', 'unknown')
            key = f"signal:{timestamp}:{symbol}"
            
            # Store signal data as JSON
            await self.redis.setex(
                key,
                7 * 24 * 3600,  # 7 days TTL
                json.dumps(signal_data)
            )
            
            # Add to sorted set for time-based queries
            score = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).timestamp()
            await self.redis.zadd("signals:by_time", {key: score})
            
            # Add to set for signal type filtering
            signal_type = signal_data.get('signal_type', 'unknown')
            await self.redis.sadd(f"signals:type:{signal_type}", key)
            
            logger.info(f"Signal saved to Redis: {symbol}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save signal to Redis: {e}")
            return False
    
    async def get_recent_signals(self, limit: int = 100, signal_type: Optional[str] = None) -> List[Dict]:
        """Get recent signals from Redis."""
        if not self._connected or not self.redis:
            return []
            
        try:
            signals = []
            
            if signal_type:
                # Get signals by type
                keys = await self.redis.smembers(f"signals:type:{signal_type}")
                keys = list(keys)[:limit]
            else:
                # Get most recent signals by time
                keys = await self.redis.zrevrange("signals:by_time", 0, limit - 1)
            
            if keys:
                values = await self.redis.mget(keys)
                for value in values:
                    if value:
                        try:
                            signals.append(json.loads(value))
                        except json.JSONDecodeError:
                            continue
            
            return signals
            
        except Exception as e:
            logger.error(f"Failed to get signals from Redis: {e}")
            return []
    
    async def get_signals_stats(self, hours: int = 24) -> Dict:
        """Get signal statistics."""
        if not self._connected or not self.redis:
            return {'total': 0, 'pumps': 0, 'dumps': 0, 'avg_score': 0}
            
        try:
            # Get all keys in time range
            now = datetime.utcnow().timestamp()
            since = now - (hours * 3600)
            keys = await self.redis.zrangebyscore("signals:by_time", since, now)
            
            total = len(keys)
            pumps = await self.redis.scard("signals:type:pump")
            dumps = await self.redis.scard("signals:type:dump")
            
            # Calculate average score
            scores = []
            if keys:
                values = await self.redis.mget(keys)
                for value in values:
                    if value:
                        try:
                            data = json.loads(value)
                            score = data.get('score', 0)
                            if score:
                                scores.append(score)
                        except:
                            continue
            
            avg_score = sum(scores) / len(scores) if scores else 0
            
            return {
                'total': total,
                'pumps': pumps,
                'dumps': dumps,
                'avg_score': round(avg_score, 2)
            }
            
        except Exception as e:
            logger.error(f"Failed to get stats from Redis: {e}")
            return {'total': 0, 'pumps': 0, 'dumps': 0, 'avg_score': 0}
    
    async def close(self):
        """Close Redis connection."""
        if self.redis:
            await self.redis.close()
            self._connected = False
            logger.info("Redis connection closed")


# Global instance
redis_store = RedisSignalsStore()
