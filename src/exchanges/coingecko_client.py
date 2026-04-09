"""CoinGecko API Client - Free tier available for market cap and trending data."""

import aiohttp
from typing import Dict, List, Optional, Any, Set
import backoff
import logging
import asyncio

logger = logging.getLogger(__name__)


class CoinGeckoClient:
    """CoinGecko API client for market cap, trending, and basic data."""
    
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
                await asyncio.sleep(30)
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

    # ------------------------------------------------------------------ #
    #  NEW: Trending                                                        #
    # ------------------------------------------------------------------ #

    async def get_trending(self) -> Dict:
        """
        Get trending coins — top 7 searched on CoinGecko in last 24h.
        FREE tier endpoint, 1 call per cycle (not per symbol).

        Response structure:
            {
              "coins": [
                {"item": {"id": "...", "symbol": "SOL", "market_cap_rank": 5, ...}},
                ...
              ]
            }
        """
        return await self._request("/search/trending")

    async def get_trending_symbols(self) -> Set[str]:
        """
        Return a set of UPPERCASE symbols currently trending on CoinGecko.

        Example return: {'SOL', 'PEPE', 'WIF', 'TIA', 'BONK', 'JUP', 'STRK'}

        HOW TO USE IN SCORER:
            trending = await coingecko.get_trending_symbols()
            base = signal.symbol.replace('USDT', '')  # 'SOLUSDT' -> 'SOL'
            if base in trending:
                factors.append('CoinGecko trending #top7')
                score += 1.0   # Strong confirmation: crowd is searching this coin NOW

        Call once per full scan cycle and cache the result — it's shared
        across all symbols, so it costs only 1 API call per 30 seconds.
        """
        try:
            data = await self.get_trending()
            symbols: Set[str] = set()
            for item in data.get('coins', []):
                coin = item.get('item', {})
                symbol = coin.get('symbol', '').upper().replace('USDT', '').replace('BUSD', '')
                if symbol:
                    symbols.add(symbol)
            logger.info(f"CoinGecko trending ({len(symbols)}): {symbols}")
            return symbols
        except Exception as e:
            logger.warning(f"Failed to fetch CoinGecko trending: {e}")
            return set()

    # ------------------------------------------------------------------ #
    #  NEW: Top gainers (free, just sorted /coins/markets)                 #
    # ------------------------------------------------------------------ #

    async def get_top_gainers_symbols(self, top_n: int = 50) -> Set[str]:
        """
        Return symbols of top N 24h gainers from CoinGecko top-250.

        HOW TO USE IN SCORER:
            gainers = await coingecko.get_top_gainers_symbols(top_n=50)
            base = signal.symbol.replace('USDT', '')
            if base in gainers:
                factors.append('Top 50 gainer 24h')
                score += 0.5   # Price already moving — momentum confirmation

        Note: this re-uses the /coins/markets call you already make for
        market cap. You can combine both into get_market_cap_and_rank_map()
        to save an API call.
        """
        try:
            markets = await self.get_coins_markets(per_page=250)
            # Sort by 24h price change descending
            sorted_markets = sorted(
                markets,
                key=lambda c: c.get('price_change_percentage_24h') or 0,
                reverse=True
            )
            return {
                coin['symbol'].upper()
                for coin in sorted_markets[:top_n]
            }
        except Exception as e:
            logger.warning(f"Failed to fetch top gainers: {e}")
            return set()

    # ------------------------------------------------------------------ #
    #  Extended market cap map (adds rank + 24h change)                    #
    # ------------------------------------------------------------------ #

    async def get_market_cap_and_rank_map(
        self, min_market_cap: float = 10_000_000
    ) -> Dict[str, Dict]:
        """
        Extended get_market_cap_map — returns per-symbol dict with:
            market_cap, rank, price_change_24h

        Replaces get_market_cap_map() — backward compatible if you only
        use result[symbol]['market_cap'].

        rank-based logic you can add to scorer:
            rank <= 50   → blue chip, lower pump probability → score *= 0.8
            51–200       → mid cap sweet spot, no penalty
            > 200        → small cap, higher volatility → score += 0.5
        """
        try:
            markets = await self.get_coins_markets(per_page=250)
        except Exception as e:
            logger.warning(f"Failed to fetch CoinGecko market cap/rank: {e}")
            return {}
        
        result = {}
        for coin in markets:
            market_cap = coin.get('market_cap', 0) or 0
            if market_cap >= min_market_cap:
                symbol = coin['symbol'].upper()
                result[symbol] = {
                    'market_cap': market_cap,
                    'rank': coin.get('market_cap_rank') or 9999,
                    'price_change_24h': coin.get('price_change_percentage_24h') or 0,
                }
        return result

    async def get_market_cap_map(self, min_market_cap: float = 10_000_000) -> Dict[str, float]:
        """Get mapping of symbol -> market cap (original method, unchanged)."""
        try:
            markets = await self.get_coins_markets(per_page=250)
        except Exception as e:
            logger.warning(f"Failed to fetch CoinGecko markets: {e}")
            return {}
        
        result = {}
        for coin in markets:
            market_cap = coin.get('market_cap', 0) or 0
            if market_cap >= min_market_cap:
                symbol = coin['symbol'].upper()
                result[symbol] = market_cap
        return result

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
    
    async def search_coins(self, query: str) -> List[Dict]:
        """Search for coins."""
        return await self._request("/search", {"query": query})
    
    def map_to_futures_symbol(self, symbol: str) -> str:
        """Map CoinGecko symbol to futures format (add USDT)."""
        special_cases = {
            "BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT",
            "SOL": "SOLUSDT", "XRP": "XRPUSDT", "DOGE": "DOGEUSDT",
            "ADA": "ADAUSDT", "AVAX": "AVAXUSDT", "TRX": "TRXUSDT",
            "DOT": "DOTUSDT", "LINK": "LINKUSDT", "MATIC": "MATICUSDT",
            "LTC": "LTCUSDT", "BCH": "BCHUSDT", "ETC": "ETCUSDT",
            "XLM": "XLMUSDT", "UNI": "UNIUSDT", "ATOM": "ATOMUSDT",
            "FIL": "FILUSDT", "ALGO": "ALGOUSDT", "VET": "VETUSDT",
            "THETA": "THETAUSDT", "XTZ": "XTZUSDT", "AXS": "AXSUSDT",
            "SAND": "SANDUSDT", "MANA": "MANAUSDT", "FTM": "FTMUSDT",
            "EOS": "EOSUSDT", "AAVE": "AAVEUSDT", "MKR": "MKRUSDT",
            "KSM": "KSMUSDT", "NEAR": "NEARUSDT", "ICP": "ICPUSDT",
            "GRT": "GRTUSDT", "SUSHI": "SUSHIUSDT", "COMP": "COMPUSDT",
            "SNX": "SNXUSDT", "CRV": "CRVUSDT", "1INCH": "1INCHUSDT",
            "DYDX": "DYDXUSDT", "ENS": "ENSUSDT", "IMX": "IMXUSDT",
            "FLOW": "FLOWUSDT", "OP": "OPUSDT", "APT": "APTUSDT",
            "ARB": "ARBUSDT", "SUI": "SUIUSDT", "SEI": "SEIUSDT",
            "TIA": "TIAUSDT", "STRK": "STRKUSDT",
        }
        if symbol in special_cases:
            return special_cases[symbol]
        return f"{symbol}USDT"
