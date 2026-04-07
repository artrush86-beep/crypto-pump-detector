"""Binance Futures API Client with proxy support for Railway deployment."""

import aiohttp
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from datetime import datetime
import backoff
import logging

from src.exchanges.proxy_session import (
    create_session,
    get_proxy_candidates,
    mark_proxy_failure,
    mark_proxy_success,
    mask_proxy,
)

logger = logging.getLogger(__name__)


@dataclass
class MarketData:
    """Unified market data structure."""
    symbol: str
    price: float
    volume_24h: float
    open_interest: float
    funding_rate: float
    long_short_ratio: float
    price_change_24h: float
    timestamp: datetime


class BinanceClient:
    """Binance USDT-M Futures API client with proxy support."""
    
    BASE_URL = "https://fapi.binance.com"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        self.weight_used = 0
        
    async def __aenter__(self):
        self.session = create_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    @backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=3)
    async def _request(self, endpoint: str, params: Dict = None) -> Any:
        """Make request to Binance API through proxy if configured."""
        url = f"{self.BASE_URL}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=15)
        last_error: Optional[Exception] = None

        for proxy in get_proxy_candidates("binance", max_candidates=3):
            kwargs = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("Binance request via proxy %s", mask_proxy(proxy))

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("Binance rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("Rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("binance", proxy)
                        logger.error("Binance 403 Forbidden - IP may be blocked. Check proxy settings.")
                        raise aiohttp.ClientError("IP blocked by Binance (403)")

                    response.raise_for_status()
                    mark_proxy_success("binance", proxy)
                    self.weight_used = int(response.headers.get('X-MBX-USED-WEIGHT-1M', 0))
                    return await response.json()
            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("binance", proxy)
                last_error = exc
                logger.warning("Binance proxy failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for Binance request")
    
    async def get_all_symbols(self) -> List[str]:
        data = await self._request("/fapi/v1/exchangeInfo")
        symbols = [
            s['symbol'] for s in data['symbols']
            if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL'
            and s['symbol'].endswith('USDT')
        ]
        return symbols[:150]
    
    async def get_all_tickers(self) -> List[Dict]:
        return await self._request("/fapi/v1/ticker/24hr")
    
    async def get_open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 2) -> List[Dict]:
        return await self._request("/futures/data/openInterestHist", {
            "symbol": symbol, "period": period, "limit": limit
        })
    
    async def get_funding_rate(self, symbol: str) -> Dict:
        return await self._request("/fapi/v1/premiumIndex", {"symbol": symbol})
    
    async def get_long_short_ratio(self, symbol: str, period: str = "15m") -> List[Dict]:
        return await self._request("/futures/data/globalLongShortAccountRatio", {
            "symbol": symbol, "period": period, "limit": 2
        })
    
    async def get_market_data_batch(self, symbols: List[str]) -> Dict[str, MarketData]:
        result = {}
        all_tickers = {t['symbol']: t for t in await self.get_all_tickers()}
        
        for i in range(0, len(symbols), 10):
            batch = symbols[i:i+10]
            tasks = []
            for symbol in batch:
                if symbol in all_tickers:
                    tasks.append(self._get_single_market_data(symbol, all_tickers[symbol]))
            
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for symbol, data in zip(batch, batch_results):
                if isinstance(data, MarketData):
                    result[symbol] = data
            
            if i + 10 < len(symbols):
                await asyncio.sleep(0.5)
        
        return result
    
    async def _get_single_market_data(self, symbol: str, ticker: Dict) -> MarketData:
        try:
            oi_hist = await self.get_open_interest_hist(symbol, "15m", 2)
            funding = await self.get_funding_rate(symbol)
            ls_ratio = await self.get_long_short_ratio(symbol)
            
            current_oi = 0
            if len(oi_hist) >= 2:
                current_oi = float(oi_hist[-1]['sumOpenInterest'])
            
            long_ratio = 1.0
            if ls_ratio and len(ls_ratio) > 0:
                long_ratio = float(ls_ratio[-1].get('longAccount', 0.5))
            
            return MarketData(
                symbol=symbol,
                price=float(ticker['lastPrice']),
                volume_24h=float(ticker['volume']),
                open_interest=current_oi,
                funding_rate=float(funding.get('lastFundingRate', 0)),
                long_short_ratio=long_ratio,
                price_change_24h=float(ticker['priceChangePercent']),
                timestamp=datetime.utcnow()
            )
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            raise
