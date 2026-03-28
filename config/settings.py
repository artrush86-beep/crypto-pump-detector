"""Configuration settings for the pump detector bot."""

from pydantic_settings import BaseSettings
from typing import List, Optional


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""
    
    # Telegram Bot
    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_CHAT_ID: str
    TELEGRAM_THREAD_ID: int = 0

    # Proxy for bypassing IP bans on Railway
    # Supports: http://user:pass@host:port, socks5://user:pass@host:port
    PROXY_URL: Optional[str] = None
    
    # API Keys (optional for most free endpoints)
    COINGLASS_API_KEY: str = ""
    
    # Detection Thresholds
    OI_CHANGE_THRESHOLD: float = 5.0
    PRICE_CHANGE_THRESHOLD: float = 1.0
    VOLUME_CHANGE_THRESHOLD: float = 50.0
    MIN_MARKET_CAP: float = 10_000_000
    
    # Time intervals (seconds)
    SCAN_INTERVAL: int = 300
    PRICE_CHECK_INTERVAL: int = 60
    LOOKBACK_WINDOW: int = 900
    
    # Signal scoring
    MIN_SIGNAL_SCORE: int = 3
    
    # Cache settings
    CACHE_TTL: int = 300
    
    # Exchange settings
    EXCHANGES: List[str] = ["binance", "bybit"]
    TOP_N_SYMBOLS: int = 100
    
    # Database
    DB_PATH: str = "data/signals.db"
    
    class Config:
        env_file = ".env"


settings = Settings()
