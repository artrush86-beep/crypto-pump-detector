"""Main application entry point."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, timedelta
from typing import Dict, Set
import time

from config.settings import settings
from src.exchanges.binance_client import BinanceClient
from src.exchanges.bybit_client import BybitClient
from src.exchanges.coingecko_client import CoinGeckoClient
from src.detector.signal_detector import SignalDetector
from src.bot.telegram_bot import SignalBot
from src.api.signals_api import SignalsAPI

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
        self.detector = SignalDetector(
            oi_threshold=settings.OI_CHANGE_THRESHOLD,
            price_threshold=settings.PRICE_CHANGE_THRESHOLD,
            volume_threshold=settings.VOLUME_CHANGE_THRESHOLD,
            min_score=settings.MIN_SIGNAL_SCORE,
            lookback_minutes=settings.LOOKBACK_WINDOW // 60
        )
        self.running = False
        self.start_time = None
        self.stats = {
            'signals_count': 0,
            'pairs_count': 0,
            'last_scan': None
        }
        self.market_caps: Dict[str, float] = {}
        self.all_symbols: Set[str] = set()
        # Initialize API server for dashboard (Railway provides PORT env var)
        import os
        port = int(os.environ.get("PORT", 8080))
        self.signals_api = SignalsAPI(host="0.0.0.0", port=port)
        logger.info(f"Signals API initialized on port {port}")
        
    async def initialize(self):
        """Initialize market data and symbols."""
        logger.info("Initializing Pump Detector...")
        
        # Get market cap data from CoinGecko
        async with CoinGeckoClient() as cg:
            logger.info("Fetching market cap data from CoinGecko...")
            self.market_caps = await cg.get_market_cap_map(
                min_market_cap=settings.MIN_MARKET_CAP
            )
            logger.info(f"Loaded {len(self.market_caps)} coins with market cap >= ${settings.MIN_MARKET_CAP:,.0f}")
        
        # Get symbols from exchanges
        try:
            async with BinanceClient() as binance:
                binance_symbols = await binance.get_all_symbols()
                logger.info(f"Binance: {len(binance_symbols)} symbols")
        except Exception as e:
            logger.error(f"Binance unavailable: {e}")
            binance_symbols = []
            
        async with BybitClient() as bybit:
            bybit_symbols = await bybit.get_all_symbols()
            logger.info(f"Bybit: {len(bybit_symbols)} symbols")
        
        # Union of symbols from both exchanges
        self.all_symbols = set(binance_symbols) | set(bybit_symbols)
        self.stats['pairs_count'] = len(self.all_symbols)
        
        logger.info(f"Total unique symbols to monitor: {len(self.all_symbols)}")
        
    async def scan_exchange(
        self,
        exchange_name: str,
        bot: SignalBot
    ):
        """Scan single exchange for signals."""
        try:
            if exchange_name == "binance":
                async with BinanceClient() as client:
                    data = await client.get_market_data_batch(list(self.all_symbols)[:100])
            elif exchange_name == "bybit":
                async with BybitClient() as client:
                    data = await client.get_market_data_batch(list(self.all_symbols)[:80])
            else:
                return
            
            if not data:
                logger.warning(f"No data from {exchange_name}")
                return
            
            logger.info(f"Scanning {exchange_name}: {len(data)} symbols")
            
            # Process and detect signals
            signals = await self.detector.process_market_data(
                exchange=exchange_name,
                data=data,
                market_caps=self.market_caps
            )
            
            if signals:
                logger.info(f"Detected {len(signals)} signals from {exchange_name}")
                await bot.send_signals_batch(signals)
                self.stats['signals_count'] += len(signals)
            
            self.stats['last_scan'] = datetime.utcnow()
            
        except Exception as e:
            logger.error(f"Error scanning {exchange_name}: {e}")
            await bot.send_error(f"{exchange_name} scan error: {str(e)[:100]}")
    
    async def run_scan_loop(self, bot: SignalBot):
        """Main scanning loop."""
        logger.info("Starting scan loop...")
        
        while self.running:
            try:
                start_time = time.time()
                
                # Scan both exchanges
                await self.scan_exchange("binance", bot)
                await asyncio.sleep(2)  # Small delay between exchanges
                await self.scan_exchange("bybit", bot)
                
                elapsed = time.time() - start_time
                sleep_time = max(0, settings.SCAN_INTERVAL - elapsed)
                
                logger.info(f"Scan completed in {elapsed:.1f}s, sleeping {sleep_time:.1f}s")
                
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
            async with SignalBot(signals_api=self.signals_api) as bot:
                await bot.start()
                
                # Start Telegram polling for button callbacks
                await bot.application.start()
                logger.info("Telegram polling started for button callbacks")
                
                self.running = True
                self.start_time = datetime.utcnow()
                
                # Run main loops (bot + API server + Telegram polling)
                await asyncio.gather(
                    self.run_scan_loop(bot),
                    self._status_loop(bot),
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
