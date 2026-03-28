"""Telegram Bot for sending pump/dump signals."""

import logging
from typing import List, Optional
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError
import asyncio
import backoff

from config.settings import settings
from src.detector.signal_detector import SignalScore

logger = logging.getLogger(__name__)


class SignalBot:
    """Telegram bot for crypto pump/dump signals."""
    
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, thread_id: Optional[int] = None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.thread_id = thread_id or settings.TELEGRAM_THREAD_ID
        self.bot: Optional[Bot] = None
        self._startup_message_sent = False
    
    async def __aenter__(self):
        self.bot = Bot(token=self.token)
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.bot:
            await self.bot.close()
    
    async def start(self):
        """Send startup message."""
        if not self._startup_message_sent:
            topic_info = f" (Topic: {self.thread_id})" if self.thread_id else ""
            await self.send_message(
                "🤖 <b>Crypto Pump Detector Started</b>\n\n"
                f"Monitoring top {settings.TOP_N_SYMBOLS} futures pairs\n"
                f"• OI Threshold: ±{settings.OI_CHANGE_THRESHOLD}%\n"
                f"• Price Threshold: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
                f"• Volume Threshold: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
                f"• Min Market Cap: ${settings.MIN_MARKET_CAP:,.0f}\n"
                f"• Min Score: {settings.MIN_SIGNAL_SCORE}/5\n"
                f"• Thread ID: {self.thread_id if self.thread_id else 'Main Chat'}{topic_info}\n\n"
                f"Scan interval: {settings.SCAN_INTERVAL}s"
            )
            self._startup_message_sent = True
            logger.info("Bot started and sent startup message")
    
    @backoff.on_exception(backoff.expo, TelegramError, max_tries=3)
    async def send_message(self, text: str):
        """Send message to configured chat."""
        try:
            kwargs = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": ParseMode.HTML,
                "disable_web_page_preview": True
            }
            # Add thread_id if specified (for group topics)
            if self.thread_id:
                kwargs["message_thread_id"] = self.thread_id
            
            await self.bot.send_message(**kwargs)
        except TelegramError as e:
            logger.error(f"Failed to send message: {e}")
            raise
    
    async def send_signal(self, signal: SignalScore):
        """Send signal alert."""
        message = signal.to_message()
        await self.send_message(message)
        logger.info(f"Sent signal for {signal.symbol}")
    
    async def send_signals_batch(self, signals: List[SignalScore]):
        """Send multiple signals."""
        if not signals:
            return
        
        # Sort by score (highest first)
        signals = sorted(signals, key=lambda x: x.score, reverse=True)
        
        for signal in signals:
            try:
                await self.send_signal(signal)
                await asyncio.sleep(0.5)  # Rate limit between messages
            except Exception as e:
                logger.error(f"Error sending signal for {signal.symbol}: {e}")
    
    async def send_status(self, stats: dict):
        """Send daily/hourly status update."""
        message = (
            f"📊 <b>Status Update</b>\n\n"
            f"Signals detected: {stats.get('signals_count', 0)}\n"
            f"Pairs monitored: {stats.get('pairs_count', 0)}\n"
            f"Uptime: {stats.get('uptime', 'N/A')}\n\n"
            f"<i>Bot is running normally</i>"
        )
        await self.send_message(message)
    
    async def send_error(self, error_msg: str):
        """Send error notification."""
        message = f"⚠️ <b>Error</b>\n\n<code>{error_msg}</code>"
        await self.send_message(message)
