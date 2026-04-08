"""Simple HTTP API for signals dashboard."""

import json
import logging
from datetime import datetime
from typing import List, Dict, Any
import aiohttp
from aiohttp import web

from src.database.signals_db import SignalsDatabase
from src.database.redis_signals import redis_store

logger = logging.getLogger(__name__)


class SignalsAPI:
    """HTTP API to serve signals for dashboard."""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8080):
        self.host = host
        self.port = port
        self.signals: List[Dict[str, Any]] = []
        self.app = web.Application()
        self.app.router.add_get("/api/signals", self.get_signals)
        self.app.router.add_get("/api/health", self.health_check)
        self.app.router.add_get("/api/stats", self.get_stats)
        self.app.router.add_options("/api/signals", self.cors_preflight)
        # Initialize database connection
        self.db = SignalsDatabase()
        self.use_redis = False
        
    async def init_redis(self):
        """Initialize Redis connection if available."""
        self.use_redis = await redis_store.connect()
        return self.use_redis
        
    async def cors_preflight(self, request: web.Request) -> web.Response:
        """Handle CORS preflight."""
        return web.Response(
            headers={
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }
        )
    
    async def get_signals(self, request: web.Request) -> web.Response:
        """Return recent signals from database."""
        # Get limit from query params
        limit = int(request.query.get("limit", 100))
        
        # Filter by type if specified
        signal_type = request.query.get("type")  # pump, dump, or None for all
        
        try:
            # Try Redis first if connected
            if self.use_redis:
                signals = await redis_store.get_recent_signals(limit=limit, signal_type=signal_type)
                # Always return from Redis if connected (even if empty)
                return web.json_response(
                    {
                        "signals": signals,
                        "total": len(signals),
                        "source": "redis"
                    },
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            
            # Fallback to SQLite database
            signals = await self.db.get_recent_signals(limit=limit, signal_type=signal_type)
            
            # Convert Row objects to dict and parse JSON factors
            processed_signals = []
            for signal in signals:
                # Convert Row to dict if needed
                if hasattr(signal, 'keys'):
                    signal_dict = {key: signal[key] for key in signal.keys()}
                else:
                    signal_dict = dict(signal)
                
                # Parse JSON factors
                if 'factors' in signal_dict and isinstance(signal_dict['factors'], str):
                    try:
                        signal_dict['factors'] = json.loads(signal_dict['factors'])
                    except:
                        signal_dict['factors'] = []
                
                # Ensure numeric fields
                for field in [
                    'oi_change',
                    'oi_change_pct',
                    'volume_change',
                    'volume_change_pct',
                    'price_change',
                    'price_change_pct',
                    'price',
                    'funding_rate',
                    'long_short_ratio',
                    'score',
                ]:
                    if field in signal_dict:
                        try:
                            signal_dict[field] = float(signal_dict[field]) if signal_dict[field] else 0
                        except:
                            signal_dict[field] = 0
                
                processed_signals.append(signal_dict)
            
            return web.json_response(
                {
                    "signals": processed_signals,
                    "total": len(processed_signals),
                    "source": "database"
                },
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error(f"Error reading from database: {e}")
            # Fallback to in-memory signals
            filtered = self.signals
            if signal_type:
                filtered = [s for s in self.signals if s.get("signal_type") == signal_type]
            
            recent = sorted(filtered, key=lambda x: x.get("timestamp", ""), reverse=True)[:limit]
            
            return web.json_response(
                {
                    "signals": recent,
                    "total": len(recent),
                    "source": "memory",
                    "error": str(e)
                },
                headers={"Access-Control-Allow-Origin": "*"}
            )
    
    async def get_stats(self, request: web.Request) -> web.Response:
        """Return signal statistics."""
        try:
            hours = int(request.query.get("hours", 24))
            
            # Try Redis first
            if self.use_redis:
                stats = await redis_store.get_signals_stats(hours=hours)
                return web.json_response(
                    {
                        "stats": stats,
                        "period_hours": hours,
                        "source": "redis"
                    },
                    headers={"Access-Control-Allow-Origin": "*"}
                )
            
            # Fallback to SQLite
            stats = await self.db.get_signals_stats(hours=hours)
            
            return web.json_response(
                {
                    "stats": stats,
                    "period_hours": hours
                },
                headers={"Access-Control-Allow-Origin": "*"}
            )
        except Exception as e:
            logger.error(f"Error getting stats: {e}")
            return web.json_response(
                {"error": str(e)},
                headers={"Access-Control-Allow-Origin": "*"},
                status=500
            )
    
    async def health_check(self, request: web.Request) -> web.Response:
        """Health check endpoint."""
        return web.json_response(
            {"status": "ok", "signals_count": len(self.signals)},
            headers={"Access-Control-Allow-Origin": "*"}
        )
    
    def add_signal(self, signal: Dict[str, Any]):
        """Add a new signal to memory and database."""
        # Add timestamp if not present
        if "timestamp" not in signal:
            signal["timestamp"] = datetime.utcnow().isoformat()
        
        # Add to in-memory list
        self.signals.insert(0, signal)
        
        # Keep only last 1000 signals in memory
        if len(self.signals) > 1000:
            self.signals = self.signals[:1000]
        
        # Persist to database (async operation in sync context)
        try:
            import asyncio
            # Create async task to save to database
            asyncio.create_task(self._save_signal_to_db(signal))
        except Exception as e:
            logger.warning(f"Failed to schedule DB save for signal: {e}")
        
        logger.info(f"Signal added to API, total in memory: {len(self.signals)}")
    
    async def _save_signal_to_db(self, signal: Dict[str, Any]):
        """Save signal to Redis and/or database."""
        errors = []
        
        # Try Redis first
        if self.use_redis:
            try:
                success = await redis_store.save_signal(signal)
                if success:
                    logger.debug(f"Signal saved to Redis: {signal.get('symbol')}")
                    return
            except Exception as e:
                errors.append(f"Redis: {e}")
        
        # Fallback to SQLite
        try:
            await self.db.save_signal(signal)
            logger.debug(f"Signal saved to SQLite: {signal.get('symbol')}")
        except Exception as e:
            errors.append(f"SQLite: {e}")
            logger.error(f"Failed to save signal: {'; '.join(errors)}")
    
    async def start(self):
        """Start the API server."""
        # Initialize Redis (optional)
        await self.init_redis()
        
        # Initialize SQLite database
        await self.db._init_db()
        
        runner = web.AppRunner(self.app)
        await runner.setup()
        
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        logger.info(f"Signals API started on http://{self.host}:{self.port}")
        logger.info(f"Dashboard URL: https://your-app-url.railway.app/api/signals")
        
        return runner
