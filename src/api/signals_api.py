"""Simple HTTP API for signals dashboard."""

import json
import logging
from datetime import datetime
from typing import List, Dict, Any
import aiohttp
from aiohttp import web

from src.database.signals_db import SignalsDatabase

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
            # Read from database
            signals = await self.db.get_recent_signals(limit=limit, signal_type=signal_type)
            
            # Parse JSON factors
            for signal in signals:
                if 'factors' in signal and isinstance(signal['factors'], str):
                    try:
                        signal['factors'] = json.loads(signal['factors'])
                    except:
                        signal['factors'] = []
            
            return web.json_response(
                {
                    "signals": signals,
                    "total": len(signals),
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
        """Add a new signal."""
        # Add timestamp if not present
        if "timestamp" not in signal:
            signal["timestamp"] = datetime.utcnow().isoformat()
        
        self.signals.insert(0, signal)
        
        # Keep only last 1000 signals
        if len(self.signals) > 1000:
            self.signals = self.signals[:1000]
        
        logger.info(f"Signal added to API, total: {len(self.signals)}")
    
    async def start(self):
        """Start the API server."""
        # Initialize database
        await self.db._init_db()
        
        runner = web.AppRunner(self.app)
        await runner.setup()
        
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        
        logger.info(f"Signals API started on http://{self.host}:{self.port}")
        logger.info(f"Dashboard URL: https://your-app-url.railway.app/api/signals")
        
        return runner
