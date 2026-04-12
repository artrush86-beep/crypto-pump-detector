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
    PROXY_URLS: str = ""
    BINANCE_PROXY_URL: Optional[str] = None
    BINANCE_PROXY_URLS: str = ""
    BYBIT_PROXY_URL: Optional[str] = None
    BYBIT_PROXY_URLS: str = ""
    PROXY_COOLDOWN_SECONDS: int = 900
    
    # API Keys (optional for most free endpoints)
    COINGLASS_API_KEY: str = ""
    
    # Detection Thresholds
    OI_CHANGE_THRESHOLD: float = 5.0
    PRICE_CHANGE_THRESHOLD: float = 1.0
    VOLUME_CHANGE_THRESHOLD: float = 50.0
    MIN_MARKET_CAP: float = 200_000
    
    # Time intervals (seconds)
    SCAN_INTERVAL: int = 180
    PRICE_CHECK_INTERVAL: int = 60
    LOOKBACK_WINDOW: int = 900
    
    # Signal scoring
    MIN_SIGNAL_SCORE: int = 3.0
    EARLY_SIGNAL_MIN_SCORE: float = 3.0
    ENABLE_EARLY_SIGNALS: bool = True
    SIGNAL_COOLDOWN_SECONDS: int = 1800
    EARLY_SIGNAL_COOLDOWN_SECONDS: int = 900

    # Notifications / bot behavior
    ERROR_NOTIFICATION_COOLDOWN_SECONDS: int = 1800
    DEFAULT_ALERT_PERCENT: float = 2.0
    
    # Cache settings
    CACHE_TTL: int = 300
    
    # Exchange settings
    EXCHANGES: List[str] = ["binance", "bybit"]
    TOP_N_SYMBOLS: int = 300
    
    # Database
    DB_PATH: str = "data/signals.db"
    
    # Redis (Upstash) for persistent signal storage
    REDIS_URL: Optional[str] = None  # e.g., rediss://default:pass@host:port
    
    class Config:
        env_file = ".env"


settings = Settings()
