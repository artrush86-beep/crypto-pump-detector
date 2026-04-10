"""Database module for storing signals and alerts."""

import aiosqlite
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class SignalsDatabase:
    """SQLite database for signal history and price alerts."""
    
    def __init__(self, db_path: str = "data/signals.db"):
        self.db_path = db_path
        self._init_done = False

    async def _table_columns(self, db: aiosqlite.Connection, table_name: str) -> List[str]:
        """Return current table columns for lightweight migrations."""
        cursor = await db.execute(f"PRAGMA table_info({table_name})")
        rows = await cursor.fetchall()
        return [row[1] for row in rows]

    async def _ensure_column(
        self,
        db: aiosqlite.Connection,
        table_name: str,
        column_name: str,
        column_sql: str,
    ) -> None:
        """Add column when upgrading an existing SQLite database."""
        columns = await self._table_columns(db, table_name)
        if column_name not in columns:
            await db.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")
            logger.info("DB migration: added %s.%s", table_name, column_name)
    
    async def _init_db(self):
        """Initialize database tables."""
        if self._init_done:
            return
            
        async with aiosqlite.connect(self.db_path) as db:
            # Signals table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    price REAL,
                    price_change_pct REAL,
                    oi_change_pct REAL,
                    volume_change_pct REAL,
                    funding_rate REAL,
                    long_short_ratio REAL,
                    factors TEXT,
                    timestamp TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await self._ensure_column(db, "signals", "timeframe", "timeframe TEXT DEFAULT '15m'")
            await self._ensure_column(db, "signals", "stage", "stage TEXT DEFAULT 'CONFIRMED'")
            await self._ensure_column(db, "signals", "confidence", "confidence TEXT DEFAULT 'LOW'")
            await self._ensure_column(db, "signals", "bias", "bias TEXT DEFAULT 'LONG'")
            
            # Price alerts table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    reference_price REAL NOT NULL,
                    target_change_pct REAL NOT NULL,
                    direction TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    thread_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    triggered_at DATETIME,
                    is_active INTEGER DEFAULT 1
                )
            """)

            # Ignored symbols
            await db.execute("""
                CREATE TABLE IF NOT EXISTS ignored_symbols (
                    symbol TEXT PRIMARY KEY,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Runtime bot state
            await db.execute("""
                CREATE TABLE IF NOT EXISTS bot_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Price history for alerts
            await db.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    exchange TEXT NOT NULL,
                    price REAL NOT NULL,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await db.execute("CREATE INDEX IF NOT EXISTS idx_signals_created_at ON signals(created_at DESC)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_active ON price_alerts(is_active)")
            await db.execute("CREATE INDEX IF NOT EXISTS idx_price_history_lookup ON price_history(symbol, exchange, timestamp DESC)")
            
            await db.commit()
            self._init_done = True
            logger.info("Database initialized")
    
    async def save_signal(self, signal_data: Dict):
        """Save signal to database."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO signals (
                    symbol, exchange, signal_type, score, price,
                    price_change_pct, oi_change_pct, volume_change_pct,
                    funding_rate, long_short_ratio, factors, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_data.get('symbol'),
                signal_data.get('exchange'),
                signal_data.get('signal_type'),
                signal_data.get('score'),
                signal_data.get('price', 0),
                signal_data.get('price_change', 0),
                signal_data.get('oi_change', 0),
                signal_data.get('volume_change', 0),
                signal_data.get('funding_rate', 0),
                signal_data.get('long_short_ratio', 1.0),
                json.dumps(signal_data.get('factors', [])),
                signal_data.get('timestamp', datetime.utcnow().isoformat())
            ))
            await db.execute(
                "UPDATE signals SET timeframe = ?, stage = ?, confidence = ?, bias = ? WHERE id = last_insert_rowid()",
                (
                    signal_data.get('timeframe', '15m'),
                    signal_data.get('stage', 'CONFIRMED'),
                    signal_data.get('confidence', 'LOW'),
                    signal_data.get('bias', 'LONG'),
                ),
            )
            await db.commit()
            logger.info(f"Signal saved to DB: {signal_data.get('symbol')}")
    
    async def get_recent_signals(self, limit: int = 100, signal_type: Optional[str] = None) -> List[Dict]:
        """Get recent signals from database."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            
            if signal_type:
                cursor = await db.execute(
                    "SELECT * FROM signals WHERE signal_type = ? ORDER BY created_at DESC LIMIT ?",
                    (signal_type, limit)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )
            
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
    
    async def get_signals_stats(self, hours: int = 24) -> Dict:
        """Get signal statistics for period."""
        await self._init_db()
        
        since = datetime.utcnow() - timedelta(hours=hours)
        
        async with aiosqlite.connect(self.db_path) as db:
            # Total signals
            cursor = await db.execute(
                "SELECT COUNT(*) FROM signals WHERE datetime(created_at) > datetime(?)",
                (since.isoformat(),)
            )
            total = (await cursor.fetchone())[0]
            
            # Pumps vs dumps
            cursor = await db.execute(
                "SELECT signal_type, COUNT(*) FROM signals WHERE datetime(created_at) > datetime(?) GROUP BY signal_type",
                (since.isoformat(),)
            )
            type_counts = {row[0]: row[1] for row in await cursor.fetchall()}
            
            # Average score
            cursor = await db.execute(
                "SELECT AVG(score) FROM signals WHERE datetime(created_at) > datetime(?)",
                (since.isoformat(),)
            )
            avg_score = (await cursor.fetchone())[0] or 0
            
            return {
                'total': total,
                'pumps': type_counts.get('PUMP', 0),
                'dumps': type_counts.get('DUMP', 0),
                'avg_score': round(avg_score, 2)
            }
    
    async def add_price_alert(self, symbol: str, exchange: str, reference_price: float, 
                               target_change_pct: float, direction: str, chat_id: str, 
                               thread_id: Optional[int] = None):
        """Add price alert."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("""
                INSERT INTO price_alerts (
                    symbol, exchange, reference_price, target_change_pct,
                    direction, chat_id, thread_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (symbol, exchange, reference_price, target_change_pct, direction, chat_id, thread_id))
            await db.commit()
            logger.info(f"Price alert added: {symbol} {direction} {target_change_pct}%")

    async def add_symmetric_price_alert(
        self,
        symbol: str,
        exchange: str,
        reference_price: float,
        target_change_pct: float,
        chat_id: str,
        thread_id: Optional[int] = None,
    ) -> None:
        """Create both up/down alerts for a symbol."""
        await self.add_price_alert(
            symbol=symbol,
            exchange=exchange,
            reference_price=reference_price,
            target_change_pct=target_change_pct,
            direction="up",
            chat_id=chat_id,
            thread_id=thread_id,
        )
        await self.add_price_alert(
            symbol=symbol,
            exchange=exchange,
            reference_price=reference_price,
            target_change_pct=target_change_pct,
            direction="down",
            chat_id=chat_id,
            thread_id=thread_id,
        )
    
    async def get_active_alerts(self) -> List[Dict]:
        """Get all active price alerts."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM price_alerts WHERE is_active = 1"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_ignored_symbol(self, symbol: str) -> None:
        """Persist symbol suppression."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO ignored_symbols (symbol) VALUES (?)",
                (symbol.upper(),),
            )
            await db.commit()
            logger.info("Ignored symbol added: %s", symbol.upper())

    async def remove_ignored_symbol(self, symbol: str) -> None:
        """Remove symbol from ignore list."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM ignored_symbols WHERE symbol = ?",
                (symbol.upper(),),
            )
            await db.commit()
            logger.info("Ignored symbol removed: %s", symbol.upper())

    async def get_ignored_symbols(self) -> List[str]:
        """Return ignored symbols."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT symbol FROM ignored_symbols ORDER BY symbol ASC")
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def is_symbol_ignored(self, symbol: str) -> bool:
        """Check whether symbol is ignored."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT 1 FROM ignored_symbols WHERE symbol = ? LIMIT 1",
                (symbol.upper(),),
            )
            return await cursor.fetchone() is not None

    async def set_bot_state(self, key: str, value: str) -> None:
        """Store arbitrary bot state values."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO bot_state (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
            await db.commit()

    async def get_bot_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Read stored bot state value."""
        await self._init_db()
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value FROM bot_state WHERE key = ? LIMIT 1",
                (key,),
            )
            row = await cursor.fetchone()
            return row[0] if row else default
    
    async def mark_alert_triggered(self, alert_id: int):
        """Mark alert as triggered."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE price_alerts SET is_active = 0, triggered_at = CURRENT_TIMESTAMP WHERE id = ?",
                (alert_id,)
            )
            await db.commit()
    
    async def save_price(self, symbol: str, exchange: str, price: float):
        """Save price snapshot."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "INSERT INTO price_history (symbol, exchange, price) VALUES (?, ?, ?)",
                (symbol, exchange, price)
            )
            await db.commit()
    
    async def get_latest_price(self, symbol: str, exchange: str) -> Optional[float]:
        """Get latest price for symbol."""
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT price FROM price_history WHERE symbol = ? AND exchange = ? ORDER BY timestamp DESC LIMIT 1",
                (symbol, exchange)
            )
            row = await cursor.fetchone()
            return row[0] if row else None
