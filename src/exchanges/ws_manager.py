"""WebSocket Manager for real-time ticker data from crypto exchanges.

Architecture:
  - One WS connection per exchange (or multiple for OKX rate limits)
  - All tickers updated in-memory every ~100ms
  - REST polling continues for OI/L/S (not available via public WS)
  - Auto-reconnect with exponential backoff
  - Heartbeat/ping-pong for connection health

Data flow:
  WS → ticker cache → detector → alerts
  REST (periodic) → OI/L/S enrichment → detector
"""

import asyncio
import json
import time
import logging
from typing import Dict, Optional, Set, Callable, Awaitable
from dataclasses import dataclass, field

try:
    import websockets
    from websockets.exceptions import (
        ConnectionClosed,
        ConnectionClosedError,
        InvalidHandshake,
    )
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
#  Ticker data from WebSocket
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class WSTicker:
    """Real-time ticker from WebSocket."""
    symbol: str          # Internal format: BTCUSDT
    price: float
    volume_24h: float    # Quote volume (USDT)
    price_change_24h: float  # Percent
    funding_rate: Optional[float] = None
    timestamp: float = 0.0   # time.time()
    exchange: str = ""


# ────────────────────────────────────────────────────────────────────────────
#  Per-exchange WebSocket handlers
# ────────────────────────────────────────────────────────────────────────────

class _BinanceWS:
    """Binance Futures WebSocket: !ticker@arr — all tickers in one stream."""

    URL = "wss://fstream.binance.com/ws/!ticker@arr"
    PING_INTERVAL = 30
    RECONNECT_DELAY_BASE = 1  # seconds, doubles on failure

    def __init__(self, ticker_callback: Callable[[str, Dict[str, WSTicker]], Awaitable[None]]):
        self._callback = ticker_callback
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay = self.RECONNECT_DELAY_BASE

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self):
        while self._running:
            try:
                async with websockets.connect(
                    self.URL,
                    ping_interval=self.PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("Binance WS connected")
                    self._reconnect_delay = self.RECONNECT_DELAY_BASE
                    async for raw in ws:
                        try:
                            data = json.loads(raw)
                            tickers = {}
                            for t in data:
                                sym = t.get("s", "")
                                if sym.endswith("USDT"):
                                    tickers[sym] = WSTicker(
                                        symbol=sym,
                                        price=float(t.get("c", 0)),
                                        volume_24h=float(t.get("q", 0)),
                                        price_change_24h=float(t.get("P", 0)),
                                        funding_rate=None,  # Not in WS ticker
                                        timestamp=time.time(),
                                        exchange="binance",
                                    )
                            if tickers:
                                await self._callback("binance", tickers)
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.debug("Binance WS parse error: %s", e)

            except (ConnectionClosed, ConnectionClosedError, InvalidHandshake, OSError) as e:
                logger.warning("Binance WS disconnected: %s — reconnecting in %ds", e, self._reconnect_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Binance WS error: %s — reconnecting in %ds", e, self._reconnect_delay)

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)


class _BybitWS:
    """Bybit V5 WebSocket: tickers topic — all linear tickers."""

    URL = "wss://stream.bybit.com/v5/public/linear"
    PING_INTERVAL = 20
    RECONNECT_DELAY_BASE = 1

    def __init__(self, ticker_callback: Callable[[str, Dict[str, WSTicker]], Awaitable[None]]):
        self._callback = ticker_callback
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay = self.RECONNECT_DELAY_BASE

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_loop(self):
        while self._running:
            try:
                async with websockets.connect(
                    self.URL,
                    ping_interval=self.PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    # Subscribe to all linear tickers
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": ["tickers"],
                    }))
                    logger.info("Bybit WS connected, subscribed to tickers")
                    self._reconnect_delay = self.RECONNECT_DELAY_BASE

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            if msg.get("topic") != "tickers":
                                continue

                            data = msg.get("data", {})
                            sym = data.get("symbol", "")
                            if not sym.endswith("USDT"):
                                continue

                            tickers = {
                                sym: WSTicker(
                                    symbol=sym,
                                    price=float(data.get("lastPrice", 0) or 0),
                                    volume_24h=float(data.get("turnover24h", 0) or 0),
                                    price_change_24h=float(data.get("price24hPcnt", 0) or 0) * 100,
                                    funding_rate=float(data.get("fundingRate", 0) or 0),
                                    timestamp=time.time(),
                                    exchange="bybit",
                                )
                            }
                            await self._callback("bybit", tickers)
                        except (json.JSONDecodeError, ValueError, TypeError) as e:
                            logger.debug("Bybit WS parse error: %s", e)

            except (ConnectionClosed, ConnectionClosedError, InvalidHandshake, OSError) as e:
                logger.warning("Bybit WS disconnected: %s — reconnecting in %ds", e, self._reconnect_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Bybit WS error: %s — reconnecting in %ds", e, self._reconnect_delay)

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)


class _OKXWS:
    """OKX WebSocket: tickers channel — subscribe per-symbol in batches.

    OKX doesn't support subscribing to all tickers at once.
    We subscribe in batches of 100 symbols.
    """

    URL = "wss://ws.okx.com:8443/ws/v5/public"
    BATCH_SIZE = 100
    PING_INTERVAL = 25
    RECONNECT_DELAY_BASE = 1

    def __init__(self, ticker_callback: Callable[[str, Dict[str, WSTicker]], Awaitable[None]]):
        self._callback = ticker_callback
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._reconnect_delay = self.RECONNECT_DELAY_BASE
        self._symbols: list = []  # Set externally before start()

    def set_symbols(self, symbols: list):
        """Set the list of OKX symbols to subscribe to."""
        self._symbols = symbols

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    @staticmethod
    def _to_okx_id(symbol: str) -> str:
        """BTCUSDT → BTC-USDT-SWAP"""
        if symbol.endswith("USDT"):
            return f"{symbol[:-4]}-USDT-SWAP"
        return f"{symbol}-SWAP"

    @staticmethod
    def _from_okx_id(inst_id: str) -> str:
        """BTC-USDT-SWAP → BTCUSDT"""
        parts = inst_id.split("-")
        if len(parts) >= 2:
            return parts[0] + parts[1]
        return inst_id

    async def _run_loop(self):
        while self._running:
            try:
                async with websockets.connect(
                    self.URL,
                    ping_interval=self.PING_INTERVAL,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    logger.info("OKX WS connected")
                    self._reconnect_delay = self.RECONNECT_DELAY_BASE

                    # Subscribe in batches
                    okx_ids = [self._to_okx_id(s) for s in self._symbols[:300]]
                    for i in range(0, len(okx_ids), self.BATCH_SIZE):
                        batch = okx_ids[i:i + self.BATCH_SIZE]
                        await ws.send(json.dumps({
                            "op": "subscribe",
                            "args": [
                                {"channel": "tickers", "instId": oid}
                                for oid in batch
                            ],
                        }))
                        logger.debug("OKX WS subscribed batch %d-%d", i, i + len(batch))
                        await asyncio.sleep(0.1)  # Avoid rate limit

                    logger.info("OKX WS subscribed %d symbols", len(okx_ids))

                    # Ping OKX every 25s to keep alive
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)

                            # Skip subscription confirmations
                            if msg.get("event") in ("subscribe", "unsubscribe", "error"):
                                if msg.get("event") == "error":
                                    logger.warning("OKX WS subscribe error: %s", msg.get("msg"))
                                continue

                            # Skip pong responses
                            if msg.get("op") == "pong":
                                continue

                            # Process ticker data
                            data_list = msg.get("data", [])
                            tickers = {}
                            for d in data_list:
                                inst_id = d.get("instId", "")
                                if not inst_id.endswith("-USDT-SWAP"):
                                    continue

                                sym = self._from_okx_id(inst_id)
                                last = float(d.get("last", 0) or 0)
                                vol = float(d.get("volCcy24h", 0) or 0)
                                open24h = float(d.get("open24h", last) or last)
                                change_pct = ((last - open24h) / open24h * 100) if open24h > 0 else 0

                                tickers[sym] = WSTicker(
                                    symbol=sym,
                                    price=last,
                                    volume_24h=vol,
                                    price_change_24h=change_pct,
                                    funding_rate=float(d.get("fundingRate", 0) or 0),
                                    timestamp=time.time(),
                                    exchange="okx",
                                )

                            if tickers:
                                await self._callback("okx", tickers)

                        except (json.JSONDecodeError, ValueError, TypeError) as e:
                            logger.debug("OKX WS parse error: %s", e)

            except (ConnectionClosed, ConnectionClosedError, InvalidHandshake, OSError) as e:
                logger.warning("OKX WS disconnected: %s — reconnecting in %ds", e, self._reconnect_delay)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("OKX WS error: %s — reconnecting in %ds", e, self._reconnect_delay)

            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)


# ────────────────────────────────────────────────────────────────────────────
#  Central WebSocket Manager
# ────────────────────────────────────────────────────────────────────────────

class ExchangeWSManager:
    """Manages WebSocket connections to all exchanges.

    Usage:
        manager = ExchangeWSManager()
        manager.set_okx_symbols(okx_symbols_list)
        await manager.start()

        # In scan loop:
        tickers = manager.get_tickers("binance")
        # ... feed to detector ...

        await manager.stop()
    """

    def __init__(self):
        self._ticker_cache: Dict[str, Dict[str, WSTicker]] = {}  # exchange -> {symbol -> WSTicker}
        self._handlers = {}
        self._running = False
        self._stats = {
            "messages_received": 0,
            "last_update": {},  # exchange -> timestamp
            "connected": {},    # exchange -> bool
        }

        if HAS_WEBSOCKETS:
            self._handlers["binance"] = _BinanceWS(self._on_tickers)
            self._handlers["bybit"] = _BybitWS(self._on_tickers)
            self._handlers["okx"] = _OKXWS(self._on_tickers)

    def set_okx_symbols(self, symbols: list):
        """Set OKX symbols for WS subscription."""
        if "okx" in self._handlers:
            self._handlers["okx"].set_symbols(symbols)

    async def _on_tickers(self, exchange: str, tickers: Dict[str, WSTicker]):
        """Callback when new tickers arrive from WS."""
        self._ticker_cache[exchange] = tickers
        self._stats["messages_received"] += 1
        self._stats["last_update"][exchange] = time.time()
        self._stats["connected"][exchange] = True

    async def start(self):
        """Start all WebSocket connections."""
        if not HAS_WEBSOCKETS:
            logger.warning("websockets package not installed — WS disabled, using REST only")
            return

        self._running = True
        for name, handler in self._handlers.items():
            try:
                await handler.start()
                self._stats["connected"][name] = True
                logger.info("Started %s WebSocket handler", name)
            except Exception as e:
                logger.error("Failed to start %s WS: %s", name, e)

    async def stop(self):
        """Stop all WebSocket connections."""
        self._running = False
        for name, handler in self._handlers.items():
            try:
                await handler.stop()
            except Exception as e:
                logger.error("Failed to stop %s WS: %s", name, e)

    def get_tickers(self, exchange: str) -> Dict[str, WSTicker]:
        """Get latest WS tickers for an exchange."""
        return self._ticker_cache.get(exchange, {})

    def get_all_tickers(self) -> Dict[str, Dict[str, WSTicker]]:
        """Get all WS tickers across all exchanges."""
        return self._ticker_cache

    def is_connected(self, exchange: str) -> bool:
        """Check if WS is connected for an exchange."""
        last = self._stats["last_update"].get(exchange, 0)
        # Consider connected if updated in last 30 seconds
        return (time.time() - last) < 30

    def get_stats(self) -> Dict:
        """Get WS statistics."""
        return {
            "connected": {
                name: self.is_connected(name)
                for name in self._handlers
            },
            "tickers_count": {
                name: len(tickers)
                for name, tickers in self._ticker_cache.items()
            },
            "messages_received": self._stats["messages_received"],
            "last_update": {
                name: time.strftime("%H:%M:%S", time.gmtime(ts))
                for name, ts in self._stats["last_update"].items()
                if ts > 0
            },
        }

    def has_data(self, exchange: str) -> bool:
        """Check if we have recent WS data for an exchange."""
        return bool(self._ticker_cache.get(exchange))
