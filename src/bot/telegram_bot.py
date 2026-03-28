"""Telegram Bot for sending pump/dump signals."""

import logging
from typing import List, Optional
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import asyncio
import backoff

from config.settings import settings
from src.detector.signal_detector import SignalScore

logger = logging.getLogger(__name__)


class SignalBot:
    """Telegram bot for crypto pump/dump signals."""
    
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, thread_id: Optional[int] = None, signals_api=None):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.thread_id = thread_id or settings.TELEGRAM_THREAD_ID
        self.bot: Optional[Bot] = None
        self._startup_message_sent = False
        self.signals_api = signals_api  # Reference to API for dashboard
        self.application: Optional[Application] = None
    
    async def __aenter__(self):
        self.bot = Bot(token=self.token)
        # Setup command handlers
        self.application = Application.builder().token(self.token).build()
        self._setup_handlers()
        await self.application.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.application:
            await self.application.shutdown()
        if self.bot:
            await self.bot.close()
    
    def _setup_handlers(self):
        """Setup command handlers."""
        self.application.add_handler(CommandHandler("start", self.cmd_start))
        self.application.add_handler(CommandHandler("help", self.cmd_help))
        self.application.add_handler(CommandHandler("status", self.cmd_status))
        self.application.add_handler(CommandHandler("settings", self.cmd_settings))
        self.application.add_handler(CommandHandler("stop", self.cmd_stop))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))
    
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        await update.message.reply_text(
            "🚀 <b>Crypto Pump Detector</b>\n\n"
            "Я отслеживаю пампы и дампы на фьючерсных биржах.\n\n"
            "<b>Команды:</b>\n"
            "📊 /status — статус бота\n"
            "⚙️ /settings — настройки\n"
            "❓ /help — помощь\n"
            "🛑 /stop — остановить бота\n\n"
            "Бот мониторит:\n"
            "• Binance Futures\n"
            "• Bybit\n\n"
            "Сигналы приходят автоматически при score ≥ 3/5",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        await update.message.reply_text(
            "❓ <b>Что означают сигналы?</b>\n\n"
            "<b>🚀 PUMP</b> — потенциальный рост цены\n"
            "<b>🔻 DUMP</b> — потенциальное падение цены\n\n"
            "<b>Метрики:</b>\n"
            "• <b>OI Change</b> — изменение открытого интереса (сколько денег в позициях)\n"
            "• <b>Price</b> — изменение цены за 24ч\n"
            "• <b>Volume</b> — рост объёма торгов\n"
            "• <b>Funding</b> — ставка фандинга (кто платит — лонгисты или шорты)\n"
            "• <b>L/S Ratio</b> — соотношение лонгов к шортам\n\n"
            "<b>Score (1-5):</b>\n"
            "• 5 = EXTREME — очень сильный сигнал\n"
            "• 4 = HIGH — сильный сигнал\n"
            "• 3 = MEDIUM — умеренный сигнал\n\n"
            "<b>Кнопки под сигналом:</b>\n"
            "📈 Chart — открыть график\n"
            "🔔 Alert — уведомить когда цена изменится\n"
            "🗑 Ignore — скрыть эту пару",
            parse_mode=ParseMode.HTML
        )
    
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command."""
        status_text = (
            "📊 <b>Статус бота</b>\n\n"
            "🟢 Бот активен\n"
            f"📡 Chat ID: <code>{self.chat_id}</code>\n"
            f"📋 Thread ID: <code>{self.thread_id or 'Main'}</code>\n\n"
            f"• Порог OI: ±{settings.OI_CHANGE_THRESHOLD}%\n"
            f"• Порог цены: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
            f"• Порог объёма: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
            f"• Минимальный score: {settings.MIN_SIGNAL_SCORE}/5\n"
            f"• Интервал сканирования: {settings.SCAN_INTERVAL}с\n\n"
            f"API: <code>/api/signals</code> — доступен для дашборда"
        )
        await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)
    
    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settings command."""
        keyboard = [
            [InlineKeyboardButton("🔔 Уведомления", callback_data='notif_settings')],
            [InlineKeyboardButton("📊 Изменить пороги", callback_data='threshold_settings')],
            [InlineKeyboardButton("🔕 Тихий режим", callback_data='silent_mode')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚙️ <b>Настройки</b>\n\n"
            "Выберите опцию:",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup
        )
    
    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /stop command."""
        await update.message.reply_text(
            "🛑 <b>Бот остановлен</b>\n\n"
            "Для запуска используйте /start",
            parse_mode=ParseMode.HTML
        )
    
    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks."""
        query = update.callback_query
        await query.answer()
        
        if query.data == 'notif_settings':
            await query.message.reply_text(
                "🔔 <b>Настройки уведомлений</b>\n\n"
                "Пока доступен только один режим — все сигналы.\n"
                "В будущем можно будет фильтровать по score.",
                parse_mode=ParseMode.HTML
            )
        elif query.data == 'threshold_settings':
            await query.message.reply_text(
                "📊 <b>Пороги сигналов</b>\n\n"
                f"Текущие настройки (меняются через Railway Variables):\n"
                f"• OI: ±{settings.OI_CHANGE_THRESHOLD}%\n"
                f"• Price: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
                f"• Volume: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
                f"• Min Score: {settings.MIN_SIGNAL_SCORE}/5",
                parse_mode=ParseMode.HTML
            )
        elif query.data == 'silent_mode':
            await query.message.reply_text(
                "🔕 <b>Тихий режим</b>\n\n"
                "Пока недоступно.\n"
                "В будущем можно будет отключить уведомления.",
                parse_mode=ParseMode.HTML
            )
        elif query.data.startswith('chart_'):
            symbol = query.data.replace('chart_', '')
            await query.message.reply_text(
                f"📈 <b>График {symbol}</b>\n\n"
                f"<a href='https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}'>TradingView</a>\n"
                f"<a href='https://coinglass.com/tv/ru/Binance_{symbol}'>CoinGlass</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )
        elif query.data.startswith('alert_'):
            symbol = query.data.replace('alert_', '')
            await query.message.reply_text(
                f"🔔 <b>Алерт для {symbol}</b>\n\n"
                f"Уведомление установлено!\n"
                f"При сильном изменении цены пришлю сообщение.",
                parse_mode=ParseMode.HTML
            )
        elif query.data.startswith('ignore_'):
            symbol = query.data.replace('ignore_', '')
            await query.message.reply_text(
                f"🗑 <b>{symbol} скрыт</b>\n\n"
                f"Эта пара больше не будет присылать сигналы.",
                parse_mode=ParseMode.HTML
            )
    
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
        """Send signal alert with buttons."""
        message = signal.to_message()
        
        # Add inline keyboard with action buttons
        keyboard = [
            [
                InlineKeyboardButton("📈 Chart", callback_data=f'chart_{signal.symbol}'),
                InlineKeyboardButton("🔔 Alert", callback_data=f'alert_{signal.symbol}'),
                InlineKeyboardButton("🗑 Ignore", callback_data=f'ignore_{signal.symbol}'),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        try:
            kwargs = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": ParseMode.HTML,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup
            }
            if self.thread_id:
                kwargs["message_thread_id"] = self.thread_id
            
            await self.bot.send_message(**kwargs)
            logger.info(f"Sent signal for {signal.symbol} with buttons")
        except TelegramError as e:
            # Fallback without buttons if error
            logger.warning(f"Failed to send with buttons, trying plain: {e}")
            await self.send_message(message)
        
        # Add to API for dashboard
        if self.signals_api:
            signal_data = {
                "symbol": signal.symbol,
                "exchange": signal.exchange,
                "signal_type": signal.signal_type,
                "score": signal.score,
                "price_change": signal.price_change_pct,
                "oi_change": signal.oi_change_pct,
                "volume_change": signal.volume_change_pct,
                "funding_rate": signal.funding_rate,
                "long_short_ratio": signal.long_short_ratio,
                "price": 0,
                "market_cap": 0
            }
            self.signals_api.add_signal(signal_data)
    
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
