"""Telegram Bot for sending pump/dump signals."""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

import backoff
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from config.settings import settings
from src.detector.signal_detector import SignalScore

logger = logging.getLogger(__name__)


class SignalBot:
    """Telegram bot for crypto pump/dump signals."""

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
        thread_id: Optional[int] = None,
        signals_api=None,
        controller=None,
    ):
        self.token = token or settings.TELEGRAM_BOT_TOKEN
        self.chat_id = chat_id or settings.TELEGRAM_CHAT_ID
        self.thread_id = thread_id or settings.TELEGRAM_THREAD_ID
        self.bot: Optional[Bot] = None
        self._startup_message_sent = False
        self.signals_api = signals_api
        self.controller = controller
        self.application: Optional[Application] = None

    async def __aenter__(self):
        self.bot = Bot(token=self.token)
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
        self.application.add_handler(CommandHandler("resume", self.cmd_resume))
        self.application.add_handler(CommandHandler("alert", self.cmd_alert))
        self.application.add_handler(CommandHandler("ignore", self.cmd_ignore))
        self.application.add_handler(CommandHandler("unignore", self.cmd_unignore))
        self.application.add_handler(CommandHandler("ignored", self.cmd_ignored))
        self.application.add_handler(CallbackQueryHandler(self.button_callback))

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command."""
        await update.message.reply_text(
            "🚀 <b>Crypto Pump Detector</b>\n\n"
            "Я отслеживаю раннее давление и подтверждённые импульсы на фьючерсных биржах.\n\n"
            "<b>Команды:</b>\n"
            "📊 /status — статус бота\n"
            "⚙️ /settings — настройки\n"
            "🔔 /alert SYMBOL % — ценовой алерт\n"
            "🙈 /ignore SYMBOL — скрыть пару\n"
            "👁 /unignore SYMBOL — вернуть пару\n"
            "📃 /ignored — список скрытых пар\n"
            "🛑 /stop — поставить сканер на паузу\n"
            "▶️ /resume — продолжить сканирование\n"
            "❓ /help — помощь\n\n"
            "Бот мониторит Binance Futures и Bybit.",
            parse_mode=ParseMode.HTML
        )

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command."""
        await update.message.reply_text(
            "❓ <b>Как читать сигналы</b>\n\n"
            "<b>🛰️ EARLY - предупреждение </b> — давление формируется заранее, цена ещё не успела полностью уйти\n"
            "<b>🚀/🔻 CONFIRMED - подтверждение </b> — импульс уже подтверждается по окну таймфрейма\n\n"
            "<b>Метрики:</b>\n"
            "• <b>OI</b> — изменение открытого интереса по реальному окну\n"
            "• <b>Цена</b> — изменение цены по текущему таймфрейму\n"
            "• <b>Объём</b> — всплеск объёма против обычного потока\n"
            "• <b>Funding</b> — перекос, кто платит\n"
            "• <b>L/S</b> — дисбаланс толпы\n\n"
            "<b>Практика:</b>\n"
            "• EARLY LONG/SHORT — смотреть график и ждать подтверждение\n"
            "• CONFIRMED LONG/SHORT — уже ближе к реальному импульсу\n"
            "• Score 4-5 — приоритет выше, но это всё равно не автосделка",
            parse_mode=ParseMode.HTML
        )

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command."""
        runtime = self.controller.runtime_status() if self.controller else {}
        exchange_symbols = runtime.get("exchange_symbols", {})
        stats = runtime.get("stats", {})
        mode = "⏸ На паузе" if runtime.get("scan_paused") else "🟢 Сканирование активно"

        status_text = (
            "📊 <b>Статус бота</b>\n\n"
            f"{mode}\n"
            f"📡 Chat ID: <code>{self.chat_id}</code>\n"
            f"📋 Thread ID: <code>{self.thread_id or 'Main'}</code>\n\n"
            f"• Порог OI: ±{settings.OI_CHANGE_THRESHOLD}%\n"
            f"• Порог цены: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
            f"• Порог объёма: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
            f"• Min score: {settings.MIN_SIGNAL_SCORE}/5\n"
            f"• Early score: {settings.EARLY_SIGNAL_MIN_SCORE}/5\n"
            f"• Интервал сканирования: {settings.SCAN_INTERVAL}с\n"
            f"• Игнорируемых пар: {runtime.get('ignored_count', 0)}\n"
            f"• Ранних сигналов: {stats.get('early_signals_count', 0)}\n"
            f"• Подтверждённых: {stats.get('confirmed_signals_count', 0)}\n"
            f"• Binance top pairs: {len(exchange_symbols.get('binance', []))}\n"
            f"• Bybit top pairs: {len(exchange_symbols.get('bybit', []))}\n\n"
            "API: <code>/api/signals</code>"
        )
        await update.message.reply_text(status_text, parse_mode=ParseMode.HTML)

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /settings command."""
        keyboard = [
            [InlineKeyboardButton("🔔 Уведомления", callback_data="notif_settings")],
            [InlineKeyboardButton("📊 Изменить пороги", callback_data="threshold_settings")],
            [InlineKeyboardButton("🔕 Тихий режим", callback_data="silent_mode")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "⚙️ <b>Настройки</b>\n\n"
            "Выберите опцию:",
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )

    async def cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Pause scanning."""
        if self.controller:
            await self.controller.set_scan_paused(True)
        await update.message.reply_text(
            "🛑 <b>Сканирование поставлено на паузу</b>\n\n"
            "Используйте /resume для продолжения.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Resume scanning."""
        if self.controller:
            await self.controller.set_scan_paused(False)
        await update.message.reply_text(
            "▶️ <b>Сканирование возобновлено</b>\n\n"
            "Бот снова будет присылать сигналы.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Create a symmetric price alert."""
        args = context.args
        if len(args) < 2:
            await update.message.reply_text(
                "🔔 <b>Создание ценового алерта</b>\n\n"
                "Использование:\n"
                "<code>/alert SYMBOL ПРОЦЕНТ</code>\n\n"
                "Примеры:\n"
                "• <code>/alert BTCUSDT 1</code>\n"
                "• <code>/alert ETHUSDT 2.5</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        symbol = args[0].upper()
        try:
            percent = abs(float(args[1]))
        except ValueError:
            await update.message.reply_text(
                "❌ Ошибка: процент должен быть числом\n"
                "Пример: <code>/alert BTCUSDT 1</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        if not self.controller:
            await update.message.reply_text("❌ Контроллер алертов недоступен.", parse_mode=ParseMode.HTML)
            return

        result = await self.controller.create_price_alert(
            symbol=symbol,
            percent=percent,
            chat_id=str(update.effective_chat.id),
            thread_id=getattr(update.effective_message, "message_thread_id", None),
        )
        if not result.get("ok"):
            await update.message.reply_text(
                "❌ <b>Не удалось создать алерт</b>\n\n"
                f"Причина: <code>{result.get('reason', 'unknown')}</code>\n"
                "Дождитесь первого скана по этой паре и попробуйте снова.",
                parse_mode=ParseMode.HTML,
            )
            return

        await update.message.reply_text(
            f"🔔 <b>Алерт создан</b>\n\n"
            f"Монета: <code>{result['symbol']}</code>\n"
            f"Биржа: <b>{result['exchange']}</b>\n"
            f"База: <code>{result['reference_price']:.6f}</code>\n"
            f"Порог: ±{result['percent']}%\n\n"
            "Уведомление придёт и на рост, и на падение.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_ignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Persist ignore for a symbol."""
        if not context.args:
            await update.message.reply_text(
                "Использование: <code>/ignore SYMBOL</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        symbol = context.args[0].upper()
        if self.controller:
            await self.controller.ignore_symbol(symbol)
        await update.message.reply_text(
            f"🙈 <b>{symbol} скрыт</b>\n\n"
            "Новые сигналы по этой паре приходить не будут.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_unignore(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Restore notifications for a symbol."""
        if not context.args:
            await update.message.reply_text(
                "Использование: <code>/unignore SYMBOL</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        symbol = context.args[0].upper()
        if self.controller:
            await self.controller.unignore_symbol(symbol)
        await update.message.reply_text(
            f"👁 <b>{symbol} возвращён</b>\n\n"
            "Сигналы по этой паре снова включены.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_ignored(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show ignored symbols."""
        ignored = await self.controller.list_ignored_symbols() if self.controller else []
        if not ignored:
            await update.message.reply_text("📃 <b>Скрытых пар нет</b>", parse_mode=ParseMode.HTML)
            return

        items = "\n".join(f"• <code>{symbol}</code>" for symbol in ignored)
        await update.message.reply_text(
            f"📃 <b>Скрытые пары</b>\n\n{items}",
            parse_mode=ParseMode.HTML,
        )

    async def button_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle inline button callbacks."""
        query = update.callback_query
        await query.answer()

        if query.data == "notif_settings":
            await query.message.reply_text(
                "🔔 <b>Уведомления</b>\n\n"
                "Сейчас включены ранние и подтверждённые сигналы.",
                parse_mode=ParseMode.HTML,
            )
        elif query.data == "threshold_settings":
            await query.message.reply_text(
                "📊 <b>Пороги сигналов</b>\n\n"
                f"• OI: ±{settings.OI_CHANGE_THRESHOLD}%\n"
                f"• Price: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
                f"• Volume: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
                f"• Min Score: {settings.MIN_SIGNAL_SCORE}/5\n"
                f"• Early Score: {settings.EARLY_SIGNAL_MIN_SCORE}/5\n"
                f"• Default Alert: ±{settings.DEFAULT_ALERT_PERCENT}%",
                parse_mode=ParseMode.HTML,
            )
        elif query.data == "silent_mode":
            await query.message.reply_text(
                "🔕 <b>Тихий режим</b>\n\n"
                "Полноценный тихий режим пока не добавлен.",
                parse_mode=ParseMode.HTML,
            )
        elif query.data.startswith("chart_"):
            symbol = query.data.replace("chart_", "")
            await query.message.reply_text(
                f"📈 <b>График {symbol}</b>\n\n"
                f"<a href='https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}'>TradingView</a>\n"
                f"<a href='https://coinglass.com/tv/ru/Binance_{symbol}'>CoinGlass</a>",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        elif query.data.startswith("alert_"):
            symbol = query.data.replace("alert_", "")
            if not self.controller:
                await query.message.reply_text("❌ Контроллер алертов недоступен.", parse_mode=ParseMode.HTML)
                return

            result = await self.controller.create_price_alert(
                symbol=symbol,
                percent=settings.DEFAULT_ALERT_PERCENT,
                chat_id=str(query.message.chat_id),
                thread_id=getattr(query.message, "message_thread_id", None),
            )
            if result.get("ok"):
                await query.message.reply_text(
                    f"🔔 <b>Алерт для {symbol}</b>\n\n"
                    f"Цена: <code>{result['reference_price']:.6f}</code>\n"
                    f"Порог: ±{result['percent']}%\n"
                    f"Биржа: <b>{result['exchange']}</b>",
                    parse_mode=ParseMode.HTML,
                )
            else:
                await query.message.reply_text(
                    f"❌ Не удалось создать алерт для <code>{symbol}</code>\n"
                    f"Причина: <code>{result.get('reason', 'unknown')}</code>",
                    parse_mode=ParseMode.HTML,
                )
        elif query.data.startswith("ignore_"):
            symbol = query.data.replace("ignore_", "")
            if self.controller:
                await self.controller.ignore_symbol(symbol)
            await query.message.reply_text(
                f"🗑 <b>{symbol} скрыт</b>\n\n"
                "Эта пара больше не будет присылать сигналы.",
                parse_mode=ParseMode.HTML,
            )

    async def start(self):
        """Send startup message."""
        if not self._startup_message_sent:
            topic_info = f" (Topic: {self.thread_id})" if self.thread_id else ""
            await self.send_message(
                "🤖 <b>Crypto Pump Detector Started</b>\n\n"
                f"Monitoring top {settings.TOP_N_SYMBOLS} futures pairs per exchange\n"
                f"• OI Threshold: ±{settings.OI_CHANGE_THRESHOLD}%\n"
                f"• Price Threshold: ±{settings.PRICE_CHANGE_THRESHOLD}%\n"
                f"• Volume Threshold: +{settings.VOLUME_CHANGE_THRESHOLD}%\n"
                f"• Min Market Cap: ${settings.MIN_MARKET_CAP:,.0f}\n"
                f"• Min Score: {settings.MIN_SIGNAL_SCORE}/5\n"
                f"• Early Score: {settings.EARLY_SIGNAL_MIN_SCORE}/5\n"
                f"• Thread ID: {self.thread_id if self.thread_id else 'Main Chat'}{topic_info}\n\n"
                f"Scan interval: {settings.SCAN_INTERVAL}s",
            )
            self._startup_message_sent = True
            logger.info("Bot started and sent startup message")

    @backoff.on_exception(backoff.expo, TelegramError, max_tries=3)
    async def send_message(
        self,
        text: str,
        chat_id: Optional[str] = None,
        thread_id: Optional[int] = None,
    ):
        """Send message to configured chat."""
        try:
            kwargs = {
                "chat_id": chat_id or self.chat_id,
                "text": text,
                "parse_mode": ParseMode.HTML,
                "disable_web_page_preview": True,
            }
            chosen_thread_id = self.thread_id if thread_id is None else thread_id
            if chosen_thread_id:
                kwargs["message_thread_id"] = chosen_thread_id

            await self.bot.send_message(**kwargs)
        except TelegramError as exc:
            logger.error("Failed to send message: %s", exc)
            raise

    async def send_signal(self, signal: SignalScore):
        """Send signal alert with action buttons."""
        message = signal.to_message()
        keyboard = [
            [
                InlineKeyboardButton("📈 Chart", callback_data=f"chart_{signal.symbol}"),
                InlineKeyboardButton("🔔 Alert", callback_data=f"alert_{signal.symbol}"),
                InlineKeyboardButton("🗑 Ignore", callback_data=f"ignore_{signal.symbol}"),
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            kwargs = {
                "chat_id": self.chat_id,
                "text": message,
                "parse_mode": ParseMode.HTML,
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            }
            if self.thread_id:
                kwargs["message_thread_id"] = self.thread_id

            await self.bot.send_message(**kwargs)
            logger.info("Sent signal for %s with buttons", signal.symbol)
        except TelegramError as exc:
            logger.warning("Failed to send with buttons, trying plain: %s", exc)
            await self.send_message(message)

        if self.signals_api:
            signal_data = {
                "symbol": signal.symbol,
                "exchange": signal.exchange,
                "signal_type": signal.signal_type,
                "score": signal.score,
                "stage": signal.stage,
                "timeframe": signal.timeframe,
                "price_change": signal.price_change_pct,
                "oi_change": signal.oi_change_pct,
                "volume_change": signal.volume_change_pct,
                "funding_rate": signal.funding_rate,
                "long_short_ratio": signal.long_short_ratio,
                "bias": signal.bias,
                "price": signal.current_price,
                "market_cap": 0,
            }
            self.signals_api.add_signal(signal_data)

    async def send_signals_batch(self, signals: List[SignalScore]):
        """Send multiple signals."""
        if not signals:
            logger.info("No signals to send to Telegram (empty list)")
            return
        
        logger.info(f"Preparing to send {len(signals)} signals to Telegram")

        signals = sorted(
            signals,
            key=lambda item: (
                item.stage != "CONFIRMED",
                -item.score,
            ),
        )

        for signal in signals:
            try:
                logger.info(f"Sending signal to Telegram: {signal.symbol} {signal.exchange} | Score: {signal.score}/5")
                await self.send_signal(signal)
                await asyncio.sleep(0.5)
            except Exception as exc:
                logger.error("Error sending signal for %s: %s", signal.symbol, exc)

    async def send_status(self, stats: dict):
        """Send periodic status update."""
        message = (
            f"📊 <b>Status Update</b>\n\n"
            f"Signals detected: {stats.get('signals_count', 0)}\n"
            f"Early signals: {stats.get('early_signals_count', 0)}\n"
            f"Confirmed signals: {stats.get('confirmed_signals_count', 0)}\n"
            f"Pairs monitored: {stats.get('pairs_count', 0)}\n"
            f"Uptime: {stats.get('uptime', 'N/A')}\n\n"
            f"<i>Bot is running normally</i>"
        )
        await self.send_message(message)

    async def send_error(self, error_msg: str):
        """Send throttled error notification."""
        message = f"⚠️ <b>Error</b>\n\n<code>{error_msg}</code>"
        await self.send_message(message)

