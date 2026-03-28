"""Bybit Futures API Client with proxy support for Railway deployment."""

import aiohttp
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import backoff
import logging

from src.exchanges.proxy_session import create_session, get_proxy_url

logger = logging.getLogger(__name__)


@dataclass
class BybitMarketData:
    symbol: str
    price: float
    volume_24h: float
    open_interest: float
    funding_rate: float
    long_short_ratio: Optional[float]
    price_change_24h: float
    timestamp: datetime


class BybitClient:
    """Bybit V5 API client with proxy support."""
    
    BASE_URL = "https://api.bybit.com"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.proxy: Optional[str] = None
        
    async def __aenter__(self):
        self.session = create_session()
        self.proxy = get_proxy_url()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    @backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=3)
    async def _request(self, endpoint: str, params: Dict = None) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        kwargs = {"params": params or {}, "timeout": aiohttp.ClientTimeout(total=15)}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        
        async with self.session.get(url, **kwargs) as response:
            if response.status == 429:
                logger.warning("Bybit rate limit hit, backing off...")
                await asyncio.sleep(1)
                raise aiohttp.ClientError("Rate limited")
            if response.status == 403:
                logger.error("Bybit 403 - IP may be blocked. Check PROXY_URL setting.")
                raise aiohttp.ClientError("IP blocked by Bybit (403)")
            
            response.raise_for_status()
            data = await response.json()
            
            if data.get('retCode') != 0:
                raise aiohttp.ClientError(f"Bybit API error: {data.get('retMsg')}")
            
            return data['result']
    
    async def get_all_symbols(self) -> List[str]:
        data = await self._request("/v5/market/instruments-info", {"category": "linear"})
        symbols = [
            item['symbol'] for item in data.get('list', [])
            if item.get('status') == 'Trading' and item.get('quoteCoin') == 'USDT'
        ]
        return symbols[:150]
    
    async def get_tickers(self, symbol: Optional[str] = None) -> List[Dict]:
        params = {"category": "linear"}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("/v5/market/tickers", params)
        return data.get('list', [])
    
    async def get_open_interest(self, symbol: str, interval: str = "15min", limit: int = 2) -> List[Dict]:
        # Bybit uses intervalTime parameter, not interval
        interval_map = {
            "5min": "5",
            "15min": "15",
            "30min": "30",
            "1h": "60",
            "4h": "240",
            "1d": "D"
        }
        bybit_interval = interval_map.get(interval, "15")
        
        data = await self._request("/v5/market/open-interest", {
            "category": "linear", 
            "symbol": symbol,
            "intervalTime": bybit_interval,  # Correct parameter name for Bybit V5
            "limit": limit
        })
        return data.get('list', [])
    
    async def get_long_short_ratio(self, symbol: str, period: str = "15min") -> List[Dict]:
        try:
            # Bybit uses periodTime parameter, not period
            period_map = {
                "5min": "5",
                "15min": "15",
                "30min": "30",
                "1h": "60",
                "4h": "240",
                "1d": "D"
            }
            bybit_period = period_map.get(period, "15")
            
            data = await self._request("/v5/market/account-ratio", {
                "category": "linear", 
                "symbol": symbol,
                "periodTime": bybit_period,  # Correct parameter name
                "limit": 2
            })
            return data.get('list', [])
        except Exception as e:
            logger.debug(f"L/S ratio not available for {symbol}: {e}")
            return []
    
    async def get_market_data_batch(self, symbols: List[str]) -> Dict[str, BybitMarketData]:
        result = {}
        all_tickers = await self.get_tickers()
        tickers_map = {t['symbol']: t for t in all_tickers}
        
        for i in range(0, len(symbols), 5):
            batch = symbols[i:i+5]
            tasks = []
            for symbol in batch:
                if symbol in tickers_map:
                    tasks.append(self._get_single_market_data(symbol, tickers_map[symbol]))
            
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, data in zip(batch, batch_results):
                if isinstance(data, BybitMarketData):
                    result[symbol] = data
            
            if i + 5 < len(symbols):
                await asyncio.sleep(0.5)
        
        return result
    
    async def _get_single_market_data(self, symbol: str, ticker: Dict) -> BybitMarketData:
        try:
            oi_hist = await self.get_open_interest(symbol, "15min", 2)
            ls_ratio = await self.get_long_short_ratio(symbol)
            
            current_oi = float(oi_hist[-1].get('openInterest', 0)) if oi_hist else 0
            
            long_ratio = None
            if ls_ratio:
                try:
                    long_ratio = float(ls_ratio[-1].get('longRatio', 0.5))
                except:
                    pass
            
            return BybitMarketData(
                symbol=symbol,
                price=float(ticker.get('lastPrice', 0)),
                volume_24h=float(ticker.get('turnover24h', ticker.get('volume24h', 0))),
                open_interest=current_oi,
                funding_rate=float(ticker.get('fundingRate', 0)),
                long_short_ratio=long_ratio,
                price_change_24h=float(ticker.get('price24hPcnt', 0)) * 100,
                timestamp=datetime.utcnow()
            )
        except Exception as e:
            logger.error(f"Error fetching Bybit {symbol}: {e}")
            raise
