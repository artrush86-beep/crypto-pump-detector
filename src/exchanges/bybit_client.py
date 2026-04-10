"""Bybit Futures API Client with proxy support for Railway deployment."""

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
class BybitMarketData:
    symbol: str
    price: float
    volume_24h: float
    open_interest: float
    funding_rate: float
    long_short_ratio: Optional[float]
    price_change_24h: float
    timestamp: datetime
    # Extended fields — mirrors Binance MarketData so detector works uniformly
    top_trader_long_short_ratio: Optional[float] = None
    taker_buy_sell_ratio: Optional[float] = None
    recent_liquidations_usd: Optional[float] = None
    liq_side: Optional[str] = None
    oi_trend: Optional[str] = None


class BybitClient:
    """Bybit V5 API client with proxy support."""
    
    BASE_URL = "https://api.bybit.com"
    
    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        
    async def __aenter__(self):
        self.session = create_session()
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    @backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError), max_tries=3)
    async def _request(self, endpoint: str, params: Dict = None) -> Any:
        url = f"{self.BASE_URL}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=15)
        last_error: Optional[Exception] = None

        for proxy in get_proxy_candidates(
            "bybit",
            max_candidates=3,
            include_direct_fallback=True,
        ):
            kwargs = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("Bybit request via proxy %s", mask_proxy(proxy))
            else:
                logger.debug("Bybit request via direct connection")

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("Bybit rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("Rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("bybit", proxy)
                        logger.error("Bybit 403 - IP may be blocked. Check proxy settings.")
                        raise aiohttp.ClientError("IP blocked by Bybit (403)")

                    response.raise_for_status()
                    data = await response.json()

                    if data.get('retCode') != 0:
                        raise aiohttp.ClientError(f"Bybit API error: {data.get('retMsg')}")

                    mark_proxy_success("bybit", proxy)
                    return data['result']
            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("bybit", proxy)
                last_error = exc
                logger.warning("Bybit route failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for Bybit request")
    
    async def get_all_symbols(self) -> List[str]:
        data = await self._request("/v5/market/instruments-info", {"category": "linear"})
        symbols = [
            item['symbol'] for item in data.get('list', [])
            if item.get('status') == 'Trading' and item.get('quoteCoin') == 'USDT'
        ]
        return symbols[:200]
    
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
                "period": period,  # Use period (e.g., "15min") not periodTime
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
            
            # OI trend from 2-period history
            oi_trend = 'flat'
            if len(oi_hist) >= 2:
                v0 = float(oi_hist[0].get('openInterest', 0))
                v1 = float(oi_hist[-1].get('openInterest', 0))
                if v1 > v0 * 1.005:
                    oi_trend = 'growing'
                elif v1 < v0 * 0.995:
                    oi_trend = 'shrinking'

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
                timestamp=datetime.utcnow(),
                # Extended: oi_trend computed above; others not available on Bybit public API
                oi_trend=oi_trend,
                top_trader_long_short_ratio=None,
                taker_buy_sell_ratio=None,
                recent_liquidations_usd=None,
                liq_side=None,
            )
        except Exception as e:
            logger.error(f"Error fetching Bybit {symbol}: {e}")
            raise
