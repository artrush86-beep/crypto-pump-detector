"""Main application entry point."""

import asyncio
import logging
import signal
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple
import time

from config.settings import settings
from src.exchanges.binance_client import BinanceClient
from src.exchanges.bybit_client import BybitClient
from src.exchanges.okx_client import OKXClient
from src.exchanges.coingecko_client import CoinGeckoClient
from src.detector.signal_detector import SignalDetector
from src.bot.telegram_bot import SignalBot
from src.api.signals_api import SignalsAPI
from src.database.signals_db import SignalsDatabase

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('logs/pump_detector.log', mode='a')
    ]
)
logger = logging.getLogger(__name__)


class PumpDetectorApp:
    """Main application orchestrator."""
    
    def __init__(self):
        # State is now managed via self.ignored_symbols and self.scan_paused
        # (persisted in SQLite via bot_state table)
        
        # Initialize detectors for multiple timeframes
        self.detectors = {
            "5m": SignalDetector(
                oi_threshold=3.0,
                price_threshold=0.5,
                volume_threshold=30.0,
                min_score=3.0,
                lookback_minutes=5
            ),
            "15m": SignalDetector(
                oi_threshold=5.0,
                price_threshold=1.0,
                volume_threshold=50.0,
                min_score=3.0,
                lookback_minutes=15
            ),
            "30m": SignalDetector(
                oi_threshold=6.5,
                price_threshold=1.5,
                volume_threshold=65.0,
                min_score=3.0,
                lookback_minutes=30
            ),
            "1h": SignalDetector(
                oi_threshold=8.0,
                price_threshold=2.0,
                volume_threshold=80.0,
                min_score=3.0,
                lookback_minutes=60
            )
        }
        self.current_timeframe = "15m"  # Default timeframe
        self.running = False
        self.start_time = None
        self.stats = {
            'signals_count': 0,
            'early_signals_count': 0,
            'confirmed_signals_count': 0,
            'pairs_count': 0,
            'last_scan': None
        }
        self.market_caps: Dict[str, float] = {}
        self.market_cap_ranks: Dict[str, int] = {}  # symbol -> CoinGecko rank
        self.trending_symbols: Set[str] = set()       # CoinGecko top-7 trending
        self.top_gainers: Set[str] = set()             # top 50 24h gainers
        self.all_symbols: Set[str] = set()
        self.exchange_symbols: Dict[str, List[str]] = {}
        self.latest_market_data: Dict[str, Dict[str, Any]] = {}
        self.ignored_symbols: Set[str] = set()
        self.scan_paused = False
        self.last_error_notifications: Dict[str, datetime] = {}
        # Initialize API server for dashboard (Railway provides PORT env var)
        import os
        port = int(os.environ.get("PORT", 8080))
        self.signals_api = SignalsAPI(host="0.0.0.0", port=port)
        self.signals_api.set_controller(self)
        # Initialize database
        self.db = SignalsDatabase()
        logger.info(f"Signals API initialized on port {port}, database ready")

    def _base_symbol(self, symbol: str) -> str:
        return symbol.replace("USDT", "").replace("USD", "")

    @staticmethod
    def _deduplicate_signals(signals: list) -> list:
        """Keep the best signal per (symbol, exchange), then boost cross-exchange.

        Step 1: per (symbol, exchange) keep best signal.
        Step 2: per (symbol) if same direction on multiple exchanges, boost score.
        """
        tf_order = {"5m": 0, "15m": 1, "30m": 2, "1h": 3}

        # Step 1: best per (symbol, exchange)
        best: dict = {}
        for sig in signals:
            key = (sig.symbol, sig.exchange)
            existing = best.get(key)
            if existing is None:
                best[key] = sig
                continue
            if sig.stage == "CONFIRMED" and existing.stage != "CONFIRMED":
                best[key] = sig
                continue
            if existing.stage == "CONFIRMED" and sig.stage != "CONFIRMED":
                continue
            if sig.score > existing.score:
                best[key] = sig
                continue
            if tf_order.get(sig.timeframe, 9) < tf_order.get(existing.timeframe, 9):
                best[key] = sig

        # Step 2: cross-exchange boost
        by_symbol: dict = {}  # symbol -> list of signals
        for sig in best.values():
            by_symbol.setdefault(sig.symbol, []).append(sig)

        for symbol, sigs in by_symbol.items():
            if len(sigs) < 2:
                continue
            # Check if same direction on multiple exchanges
            directions = {s.signal_type for s in sigs}
            if len(directions) == 1:
                # All same direction — boost best signal by 0.5
                best_sig = max(sigs, key=lambda s: s.score)
                best_sig.score = min(best_sig.score + 0.5, 5.0)
                best_sig.details.setdefault("factors", []).append(
                    f"Cross-exchange confirmation ({len(sigs)} exchanges)"
                )

        return list(best.values())

    def _select_top_symbols(self, symbols: List[str]) -> List[str]:
        """Select a stable top-N list ordered by market cap."""
        # If market_caps is empty (CoinGecko failed), skip market cap filtering
        if not self.market_caps:
            logger.warning("No market cap data available, selecting top symbols without market cap filter")
            return sorted(symbols)[:settings.TOP_N_SYMBOLS]
        
        # FIX: unknown symbols (not in CoinGecko top-250) get MIN_MARKET_CAP as default.
        # Previously used 0 → caused ~125 valid Binance symbols to be silently dropped.
        # This matches the same logic in signal_detector.process_market_data.
        filtered = [
            symbol for symbol in symbols
            if self.market_caps.get(self._base_symbol(symbol), settings.MIN_MARKET_CAP) >= settings.MIN_MARKET_CAP
        ]
        ordered = sorted(
            filtered,
            key=lambda item: (
                -self.market_caps.get(self._base_symbol(item), 0),
                item,
            ),
        )
        return ordered[:settings.TOP_N_SYMBOLS]

    async def _load_persistent_state(self) -> None:
        """Load ignore list and paused state.

        Tries Redis first (survives Railway restarts), falls back to SQLite.
        """
        from src.database.redis_signals import redis_store

        if redis_store._connected:
            try:
                self.ignored_symbols = await redis_store.load_ignored_symbols()
                paused_raw = await redis_store.load_state("scan_paused", "0")
                self.scan_paused = paused_raw == "1"
                logger.info(
                    "Loaded state from Redis: ignored=%s paused=%s",
                    len(self.ignored_symbols), self.scan_paused,
                )
                return
            except Exception as e:
                logger.warning(f"Redis state load failed, falling back to SQLite: {e}")

        # Fallback: SQLite (ephemeral on Railway)
        self.ignored_symbols = set(await self.db.get_ignored_symbols())
        self.scan_paused = (await self.db.get_bot_state("scan_paused", "0")) == "1"
        logger.info(
            "Loaded state from SQLite: ignored=%s paused=%s",
            len(self.ignored_symbols), self.scan_paused,
        )

    def _get_latest_symbol_snapshot(self, symbol: str) -> Tuple[Optional[str], Optional[Any]]:
        """Return latest market snapshot for symbol, preferring Binance then Bybit."""
        upper_symbol = symbol.upper()
        for exchange_name in ("binance", "bybit", "okx"):
            snapshot = self.latest_market_data.get(exchange_name, {}).get(upper_symbol)
            if snapshot:
                return exchange_name, snapshot
        return None, None

    async def create_price_alert(
        self,
        symbol: str,
        percent: float,
        chat_id: str,
        thread_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Create a real symmetric price alert from the latest cached quote."""
        exchange_name, snapshot = self._get_latest_symbol_snapshot(symbol)
        if not snapshot:
            return {
                "ok": False,
                "reason": "pair not cached yet",
            }

        current_price = getattr(snapshot, "price", 0.0)
        if not current_price:
            return {
                "ok": False,
                "reason": "price unavailable",
            }

        await self.db.add_symmetric_price_alert(
            symbol=symbol.upper(),
            exchange=exchange_name,
            reference_price=current_price,
            target_change_pct=percent,
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return {
            "ok": True,
            "symbol": symbol.upper(),
            "exchange": exchange_name,
            "reference_price": current_price,
            "percent": percent,
        }

    async def _persist_state(self) -> None:
        """Save current state to Redis (survives restarts) and SQLite (fallback)."""
        from src.database.redis_signals import redis_store

        # Always update in-memory
        # SQLite fallback
        await self.db.add_ignored_symbols_batch(self.ignored_symbols)
        await self.db.set_bot_state("scan_paused", "1" if self.scan_paused else "0")

        # Redis (persistent across Railway restarts)
        if redis_store._connected:
            try:
                await redis_store.save_ignored_symbols(self.ignored_symbols)
                await redis_store.save_state(
                    "scan_paused", "1" if self.scan_paused else "0"
                )
            except Exception as e:
                logger.warning(f"Failed to persist state to Redis: {e}")

    async def ignore_symbol(self, symbol: str) -> None:
        upper_symbol = symbol.upper()
        self.ignored_symbols.add(upper_symbol)
        await self._persist_state()

    async def unignore_symbol(self, symbol: str) -> None:
        upper_symbol = symbol.upper()
        self.ignored_symbols.discard(upper_symbol)
        await self._persist_state()

    async def list_ignored_symbols(self) -> List[str]:
        return sorted(self.ignored_symbols)

    async def set_scan_paused(self, paused: bool) -> None:
        self.scan_paused = paused
        await self._persist_state()

    def runtime_status(self) -> Dict[str, Any]:
        """Snapshot for Telegram commands."""
        return {
            "scan_paused": self.scan_paused,
            "ignored_count": len(self.ignored_symbols),
            "exchange_symbols": self.exchange_symbols,
            "stats": self.stats,
        }

    def _should_notify_error(self, exchange_name: str, message: str) -> bool:
        """Throttle repetitive error notifications to Telegram."""
        normalized = f"{exchange_name}:{message[:80]}"
        now = datetime.utcnow()
        last_sent = self.last_error_notifications.get(normalized)
        if last_sent:
            elapsed = (now - last_sent).total_seconds()
            if elapsed < settings.ERROR_NOTIFICATION_COOLDOWN_SECONDS:
                return False
        self.last_error_notifications[normalized] = now
        return True
        
    async def initialize(self):
        """Initialize market data and symbols."""
        logger.info("Initializing Pump Detector...")
        await self._load_persistent_state()
        
        # Get market cap data from CoinGecko (with fallback)
        try:
            async with CoinGeckoClient() as cg:
                logger.info("Fetching market cap + trending from CoinGecko...")

                # Extended market cap with rank
                cap_rank_map = await cg.get_market_cap_and_rank_map(
                    min_market_cap=settings.MIN_MARKET_CAP
                )
                self.market_caps = {
                    sym: data["market_cap"] for sym, data in cap_rank_map.items()
                }
                self.market_cap_ranks = {
                    sym: data["rank"] for sym, data in cap_rank_map.items()
                }
                logger.info(
                    "Loaded %d coins with market cap >= $%s",
                    len(self.market_caps),
                    f"{settings.MIN_MARKET_CAP:,.0f}",
                )

                # Trending: top 7 searched on CoinGecko (1 API call)
                try:
                    self.trending_symbols = await cg.get_trending_symbols()
                    logger.info("CoinGecko trending: %s", self.trending_symbols)
                except Exception as e:
                    logger.warning("CoinGecko trending failed: %s", e)
                    self.trending_symbols = set()

                # Top gainers: top 50 by 24h change (reuses market data)
                try:
                    self.top_gainers = await cg.get_top_gainers_symbols(top_n=50)
                    logger.info("CoinGecko top gainers: %d symbols", len(self.top_gainers))
                except Exception as e:
                    logger.warning("CoinGecko gainers failed: %s", e)
                    self.top_gainers = set()

        except Exception as e:
            logger.warning(f"CoinGecko failed (rate limit?), continuing without market cap filter: {e}")
            self.market_caps = {}
        
        # Get symbols from exchanges
        try:
            async with BinanceClient() as binance:
                binance_symbols = await binance.get_all_symbols()
                logger.info(f"Binance: {len(binance_symbols)} symbols")
        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"Binance unavailable: {error_detail}")
            binance_symbols = []
            
        try:
            async with BybitClient() as bybit:
                bybit_symbols = await bybit.get_all_symbols()
                logger.info(f"Bybit: {len(bybit_symbols)} symbols")
        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"Bybit unavailable during init: {error_detail}")
            bybit_symbols = []

        try:
            async with OKXClient() as okx:
                okx_symbols = await okx.get_all_symbols()
                logger.info(f"OKX: {len(okx_symbols)} symbols")
        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error(f"OKX unavailable during init: {error_detail}")
            okx_symbols = []
        
        self.exchange_symbols = {
            "binance": self._select_top_symbols(binance_symbols),
            "bybit": self._select_top_symbols(bybit_symbols),
            "okx": self._select_top_symbols(okx_symbols),
        }
        self.latest_market_data = {"binance": {}, "bybit": {}, "okx": {}}

        self.all_symbols = (
            set(self.exchange_symbols["binance"])
            | set(self.exchange_symbols["bybit"])
            | set(self.exchange_symbols["okx"])
        )
        self.stats['pairs_count'] = len(self.all_symbols)
        
        logger.info(
            "Selected pairs: Binance=%s Bybit=%s OKX=%s Total unique=%s",
            len(self.exchange_symbols["binance"]),
            len(self.exchange_symbols["bybit"]),
            len(self.exchange_symbols["okx"]),
            len(self.all_symbols),
        )
        
    async def scan_exchange(
        self,
        exchange_name: str,
        bot: SignalBot
    ):
        """Scan single exchange for signals."""
        try:
            symbols = self.exchange_symbols.get(exchange_name, [])
            if not symbols:
                logger.warning("No symbols configured for %s", exchange_name)
                return

            if exchange_name == "binance":
                async with BinanceClient() as client:
                    data = await client.get_market_data_batch(symbols)
            elif exchange_name == "bybit":
                async with BybitClient() as client:
                    data = await client.get_market_data_batch(symbols)
            elif exchange_name == "okx":
                async with OKXClient() as client:
                    data = await client.get_market_data_batch(symbols)
            else:
                return
            
            if not data:
                logger.warning(f"No data from {exchange_name}")
                return

            self.latest_market_data[exchange_name] = data
            
            logger.info(f"Scanning {exchange_name}: {len(data)} symbols")
            
            # Process each timeframe and collect all signals
            all_signals = []
            for timeframe, detector in self.detectors.items():
                logger.info(f"Processing {timeframe} timeframe...")
                
                # Process and detect signals for this timeframe
                signals = await detector.process_market_data(
                    exchange=exchange_name,
                    data=data,
                    market_caps=self.market_caps,
                    trending=self.trending_symbols,
                    top_gainers=self.top_gainers,
                    ranks=self.market_cap_ranks,
                )
                
                if signals:
                    logger.info(f"✅ Detected {len(signals)} signals from {exchange_name} ({timeframe})")
                    for sig in signals:
                        logger.info(f"  -> Signal: {sig.symbol} | Score: {sig.score}/5 | Type: {sig.signal_type}")
                    
                    # Add timeframe to each signal
                    for signal in signals:
                        signal.timeframe = timeframe
                    all_signals.extend(signals)
                else:
                    logger.info(f"❌ No signals detected for {exchange_name} ({timeframe})")

            # Filter ignored symbols
            filtered_signals = [
                signal for signal in all_signals
                if signal.symbol not in self.ignored_symbols
            ]
            logger.info(f"📊 After filtering: {len(filtered_signals)} signals ready for Telegram")

            # Deduplicate: keep best signal per symbol (highest score, prefer CONFIRMED)
            deduped = self._deduplicate_signals(filtered_signals)
            if len(deduped) < len(filtered_signals):
                logger.info(
                    "🔁 Deduplicated %d → %d signals (kept best per symbol)",
                    len(filtered_signals), len(deduped),
                )

            # Send to Telegram
            if bot and deduped:
                logger.info(f"Sending {len(deduped)} signals to Telegram")
                await bot.send_signals_batch(deduped)
            elif not all_signals:
                logger.info(
                    "No signals detected — normal on first scan (need baseline data from previous scan)"
                )
            else:
                logger.info("Signals found but filtered out (ignored symbols or deduplication)")
            
            # Check price alerts
            await self._check_price_alerts(exchange_name, data, bot)
            
            self.stats['last_scan'] = datetime.utcnow()
            
        except Exception as e:
            error_msg = str(e).strip()
            if isinstance(e, asyncio.TimeoutError):
                error_detail = f"TimeoutError: {exchange_name} API timed out (all proxies failed or network issue)"
            elif error_msg:
                error_detail = f"{type(e).__name__}: {error_msg}"
            else:
                error_detail = f"{type(e).__name__}: (no details)"
            logger.error(f"Error scanning {exchange_name}: {error_detail}")
            if self._should_notify_error(exchange_name, error_detail):
                await bot.send_error(f"{exchange_name} scan error: {error_detail[:160]}")
    
    async def _check_price_alerts(self, exchange_name: str, data: Dict, bot: SignalBot):
        """Check price alerts and trigger if threshold reached."""
        try:
            alerts = await self.db.get_active_alerts()
            if not alerts:
                return
            
            for alert in alerts:
                symbol = alert['symbol']
                exchange = alert['exchange']

                if exchange != exchange_name:
                    continue
                
                # Find symbol in current data
                market_data = data.get(symbol)
                if not market_data:
                    continue
                
                current_price = getattr(market_data, 'price', 0)
                if not current_price:
                    continue
                
                reference_price = alert['reference_price']
                target_change = alert['target_change_pct']
                direction = alert['direction']
                
                # Calculate price change
                price_change_pct = ((current_price - reference_price) / reference_price) * 100
                
                # Check if alert triggered
                triggered = False
                if direction == 'up' and price_change_pct >= target_change:
                    triggered = True
                elif direction == 'down' and price_change_pct <= -target_change:
                    triggered = True
                
                if triggered:
                    # Send alert
                    await bot.send_message(
                        f"🔔 <b>ЦЕНОВОЙ АЛЕРТ СРАБОТАЛ!</b>\n\n"
                        f"<b>{symbol}</b> ({exchange_name})\n"
                        f"Цена изменилась на {price_change_pct:+.2f}%\n"
                        f"Было: ${reference_price:.4f}\n"
                        f"Сейчас: ${current_price:.4f}\n\n"
                        f"Алерт удалён."
                        ,
                        chat_id=alert['chat_id'],
                        thread_id=alert.get('thread_id'),
                    )
                    # Mark as triggered
                    await self.db.mark_alert_triggered(alert['id'])
                    logger.info(f"Price alert triggered: {symbol} {price_change_pct:+.2f}%")
                    
        except Exception as e:
            logger.error(f"Error checking price alerts: {e}")
    
    async def run_scan_loop(self, bot: SignalBot):
        """Main scanning loop."""
        logger.info("Starting scan loop...")
        
        while self.running:
            try:
                if self.scan_paused:
                    logger.info("Scan paused by operator")
                    await asyncio.wait_for(self._stop_event.wait(), timeout=10)
                    continue

                start_time = time.time()
                
                # Scan ALL exchanges with staggered start to avoid rate limits.
                # Binance first (likely to fail fast without proxies),
                # then Bybit/OKX 2s later to spread API load.
                exchange_names = settings.exchanges_list
                scan_tasks = []
                for idx, name in enumerate(exchange_names):
                    if idx > 0:
                        await asyncio.sleep(2)  # stagger start
                    scan_tasks.append(self.scan_exchange(name, bot))
                # Wait for all to complete
                scan_results = await asyncio.gather(*scan_tasks, return_exceptions=True)
                
                for name, result in zip(exchange_names, scan_results):
                    if isinstance(result, Exception):
                        logger.error(f"Failed to scan {name}: {result}")
                
                elapsed = time.time() - start_time
                sleep_time = max(0, settings.SCAN_INTERVAL - elapsed)
                
                logger.info(
                    "Parallel scan completed in %.1fs (%d exchanges), sleeping %.1fs",
                    elapsed, len(exchange_names), sleep_time,
                )
                
                # Sleep with interrupt check
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=sleep_time
                )
                
            except asyncio.TimeoutError:
                continue  # Normal timeout, continue loop
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                await asyncio.sleep(10)
    
    async def _status_loop(self, bot: SignalBot):
        """Send periodic status updates."""
        while self.running:
            try:
                await asyncio.sleep(3600)  # Every hour
                
                if self.start_time:
                    uptime = datetime.utcnow() - self.start_time
                    self.stats['uptime'] = str(uptime).split('.')[0]
                
                await bot.send_status(self.stats)
                
            except Exception as e:
                logger.error(f"Status loop error: {e}")

    async def _symbol_refresh_loop(self):
        """Refresh exchange symbol lists every 6 hours.

        If an exchange was down at startup, its symbols list stays empty
        until container restart.  This loop fixes that by periodically
        re-fetching and updating the live symbol sets.
        """
        REFRESH_INTERVAL = 6 * 3600  # 6 hours
        while self.running:
            try:
                await asyncio.sleep(REFRESH_INTERVAL)
                logger.info("Refreshing exchange symbol lists...")

                for exchange_name in settings.exchanges_list:
                    try:
                        if exchange_name == "binance":
                            async with BinanceClient() as client:
                                symbols = await client.get_all_symbols()
                        elif exchange_name == "bybit":
                            async with BybitClient() as client:
                                symbols = await client.get_all_symbols()
                        elif exchange_name == "okx":
                            async with OKXClient() as client:
                                symbols = await client.get_all_symbols()
                        else:
                            continue

                        selected = self._select_top_symbols(symbols)
                        old_count = len(self.exchange_symbols.get(exchange_name, []))
                        self.exchange_symbols[exchange_name] = selected
                        self.stats['pairs_count'] = len(
                            set().union(*self.exchange_symbols.values())
                        )
                        logger.info(
                            "Refreshed %s: %d → %d symbols",
                            exchange_name, old_count, len(selected),
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to refresh %s symbols: %s",
                            exchange_name, e,
                        )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Symbol refresh loop error: {e}")
    
    async def run(self):
        """Main entry point."""
        logger.info("="*50)
        logger.info("Crypto Pump Detector Starting")
        logger.info("="*50)
        
        # Setup stop event
        self._stop_event = asyncio.Event()
        
        # Handle signals (works on Linux/Mac, may not work on Windows)
        def signal_handler():
            logger.info("Shutdown signal received")
            self.running = False
            self._stop_event.set()
        
        try:
            # Try to add signal handlers (fails on Windows for some signals)
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    asyncio.get_event_loop().add_signal_handler(sig, signal_handler)
                except (NotImplementedError, ValueError):
                    # Windows doesn't support add_signal_handler for some signals
                    pass
        except Exception as e:
            logger.warning(f"Signal handler setup skipped: {e}")
        
        try:
            # Start API server FIRST for Railway health check
            api_runner = await self.signals_api.start()
            logger.info("API server started - Railway health check should pass now")
            
            # Give Railway a moment to register the port
            await asyncio.sleep(2)
            
            # Initialize market data
            await self.initialize()
            
            # Start bot with API reference for signal tracking
            async with SignalBot(signals_api=self.signals_api, controller=self) as bot:
                await bot.start()
                
                # Start Telegram polling for button callbacks
                await bot.application.start()
                logger.info("Telegram polling started for button callbacks")
                
                self.running = True
                self.start_time = datetime.utcnow()
                
                # Run main loops (bot + API server + Telegram polling + symbol refresh)
                await asyncio.gather(
                    self.run_scan_loop(bot),
                    self._status_loop(bot),
                    self._symbol_refresh_loop(),
                    bot.application.updater.start_polling(),
                    return_exceptions=True
                )
                
        except Exception as e:
            logger.error(f"Fatal error: {e}")
            raise
        finally:
            logger.info("Pump Detector stopped")
            # Cleanup API server
            if 'api_runner' in locals():
                await api_runner.cleanup()


async def main():
    """Entry point."""
    import os
    
    # Create logs directory
    os.makedirs('logs', exist_ok=True)
    os.makedirs('data', exist_ok=True)
    
    app = PumpDetectorApp()
    await app.run()


if __name__ == "__main__":
    asyncio.run(main())
