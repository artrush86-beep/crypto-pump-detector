"""CoinGecko API Client - Free tier available for market cap data."""

import aiohttp
from typing import Dict, List, Optional, Any
import backoff
import logging
import asyncio

logger = logging.getLogger(__name__)


class CoinGeckoClient:
    """CoinGecko API client for market cap and basic data."""
    
    BASE_URL = "https://api.coingecko.com/api/v3"
    
    def __init__(self, api_key: Optional[str] = None):
        self.session: Optional[aiohttp.ClientSession] = None
        self.api_key = api_key
        self._cache = {}
        self._last_request_time = 0
        self._min_interval = 1.2  # Free tier: ~50 calls/minute max
        
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    async def _rate_limit(self):
        """Respect rate limits."""
        import time
        elapsed = time.time() - self._last_request_time
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request_time = time.time()
    
    @backoff.on_exception(backoff.expo, aiohttp.ClientError, max_tries=3)
    async def _request(self, endpoint: str, params: Dict = None) -> Any:
        """Make request to CoinGecko API."""
        await self._rate_limit()
        
        url = f"{self.BASE_URL}{endpoint}"
        headers = {}
        
        if self.api_key:
            headers["x-cg-pro-api-key"] = self.api_key
        
        async with self.session.get(url, params=params or {}, headers=headers) as response:
            if response.status == 429:
                logger.warning("CoinGecko rate limit hit, backing off...")
                await asyncio.sleep(30)  # Wait longer for CoinGecko
                raise aiohttp.ClientError("Rate limited")
            
            response.raise_for_status()
            return await response.json()
    
    async def get_coins_markets(
        self, 
        vs_currency: str = "usd",
        per_page: int = 250,
        page: int = 1
    ) -> List[Dict]:
        """Get coins with market data including market cap."""
        params = {
            "vs_currency": vs_currency,
            "order": "market_cap_desc",
            "per_page": per_page,
            "page": page,
            "sparkline": "false",
            "price_change_percentage": "24h"
        }
        
        return await self._request("/coins/markets", params)
    
    async def get_coin_by_id(self, coin_id: str) -> Dict:
        """Get detailed data for specific coin."""
        params = {
            "localization": "false",
            "tickers": "false",
            "market_data": "true",
            "community_data": "false",
            "developer_data": "false"
        }
        
        return await self._request(f"/coins/{coin_id}", params)
    
    async def get_market_cap_map(self, min_market_cap: float = 10_000_000) -> Dict[str, float]:
        """Get mapping of symbol -> market cap, filtering by minimum."""
        markets = await self.get_coins_markets(per_page=250)
        
        result = {}
        for coin in markets:
            market_cap = coin.get('market_cap', 0) or 0
            if market_cap >= min_market_cap:
                symbol = coin['symbol'].upper()
                result[symbol] = market_cap
        
        return result
    
    async def search_coins(self, query: str) -> List[Dict]:
        """Search for coins."""
        return await self._request("/search", {"query": query})
    
    def map_to_futures_symbol(self, symbol: str) -> str:
        """Map CoinGecko symbol to futures format (add USDT)."""
        # Common mappings
        special_cases = {
            "BTC": "BTCUSDT",
            "ETH": "ETHUSDT",
            "BNB": "BNBUSDT",
            "SOL": "SOLUSDT",
            "XRP": "XRPUSDT",
            "DOGE": "DOGEUSDT",
            "ADA": "ADAUSDT",
            "AVAX": "AVAXUSDT",
            "TRX": "TRXUSDT",
            "DOT": "DOTUSDT",
            "LINK": "LINKUSDT",
            "MATIC": "MATICUSDT",
            "LTC": "LTCUSDT",
            "BCH": "BCHUSDT",
            "ETC": "ETCUSDT",
            "XLM": "XLMUSDT",
            "UNI": "UNIUSDT",
            "ATOM": "ATOMUSDT",
            "FIL": "FILUSDT",
            "ALGO": "ALGOUSDT",
            "VET": "VETUSDT",
            "THETA": "THETAUSDT",
            "XTZ": "XTZUSDT",
            "AXS": "AXSUSDT",
            "SAND": "SANDUSDT",
            "MANA": "MANAUSDT",
            "FTM": "FTMUSDT",
            "EOS": "EOSUSDT",
            "AAVE": "AAVEUSDT",
            "MKR": "MKRUSDT",
            "KSM": "KSMUSDT",
            "NEAR": "NEARUSDT",
            "ICP": "ICPUSDT",
            "GRT": "GRTUSDT",
            "SUSHI": "SUSHIUSDT",
            "COMP": "COMPUSDT",
            "SNX": "SNXUSDT",
            "CRV": "CRVUSDT",
            "1INCH": "1INCHUSDT",
            "DYDX": "DYDXUSDT",
            "ENS": "ENSUSDT",
            "IMX": "IMXUSDT",
            "FLOW": "FLOWUSDT",
            "OP": "OPUSDT",
            "APT": "APTUSDT",
            "ARB": "ARBUSDT",
            "SUI": "SUIUSDT",
            "SEI": "SEIUSDT",
            "TIA": "TIAUSDT",
            "STRK": "STRKUSDT",
        }
        
        if symbol in special_cases:
            return special_cases[symbol]
        
        return f"{symbol}USDT"
