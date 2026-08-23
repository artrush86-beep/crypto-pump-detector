"""Bitget USDT-M Futures API Client with proxy support.

Symbol convention:
  Bitget uses BTCUSDT  (same as Binance)
  No conversion needed.
"""

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
    TTLCache,
)

logger = logging.getLogger(__name__)


@dataclass
class BitgetMarketData:
    """Market data from Bitget. Same interface as Binance/Bybit/OKX."""
    symbol: str
    price: float
    volume_24h: float
    open_interest: float
    funding_rate: float
    long_short_ratio: Optional[float]
    price_change_24h: float
    timestamp: datetime
    top_trader_long_short_ratio: Optional[float] = None
    taker_buy_sell_ratio: Optional[float] = None
    recent_liquidations_usd: Optional[float] = None
    liq_side: Optional[str] = None
    oi_trend: Optional[str] = None


def _safe_float(val, default: float = 0.0) -> float:
    """Convert to float safely."""
    if val is None or val == '':
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class BitgetClient:
    """Bitget V2 API client with proxy support."""

    BASE_URL = "https://api.bitget.com"
    _symbol_cache = TTLCache(default_ttl=600)

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
            "bitget",
            max_candidates=3,
            include_direct_fallback=True,
            direct_first=True,
        ):
            kwargs: Dict[str, Any] = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("Bitget request via proxy %s", mask_proxy(proxy))
            else:
                logger.debug("Bitget request via direct connection")

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("Bitget rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("Rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("bitget", proxy)
                        logger.error("Bitget 403 - IP blocked.")
                        raise aiohttp.ClientError("IP blocked by Bitget (403)")

                    response.raise_for_status()
                    data = await response.json()

                    code = data.get("code", "00000")
                    if code != "00000":
                        raise aiohttp.ClientError(
                            f"Bitget API error {code}: {data.get('msg', '')}"
                        )

                    mark_proxy_success("bitget", proxy)
                    return data.get("data", {})

            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("bitget", proxy)
                last_error = exc
                logger.warning("Bitget route failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for Bitget request")

    async def get_all_symbols(self) -> List[str]:
        """Return list of active USDT perpetual symbols."""
        data = await self._request(
            "/api/v2/mix/market/contracts",
            {"productType": "USDT-FUTURES"},
        )
        items = data if isinstance(data, list) else data.get("data", [])
        symbols = [
            item["symbol"]
            for item in items
            if item.get("quoteCoin") == "USDT"
            and item.get("symbolType") == "perpetual"
        ]
        return symbols[:300]

    async def get_tickers(self) -> List[Dict]:
        """Batch ticker for all USDT-FUTURES instruments."""
        data = await self._request(
            "/api/v2/mix/market/tickers",
            {"productType": "USDT-FUTURES"},
        )
        items = data if isinstance(data, list) else data.get("data", [])
        return items

    async def get_open_interest(self, symbol: str) -> List[Dict]:
        """OI history for a symbol."""
        try:
            data = await self._request(
                "/api/v2/mix/market/open-interest",
                {
                    "productType": "USDT-FUTURES",
                    "symbol": symbol,
                    "granularity": "15min",
                    "limit": "2",
                },
            )
            oi_list = data.get("openInterestList", []) if isinstance(data, dict) else []
            return [{"openInterest": item.get("size", 0)} for item in oi_list]
        except Exception as e:
            logger.debug("Bitget OI not available for %s: %s", symbol, e)
            return []

    async def get_market_data_batch(self, symbols: List[str]) -> Dict[str, BitgetMarketData]:
        """Two-step fetch: cheap tickers first, then OI only for candidates."""
        result = {}
        all_tickers = await self.get_tickers()
        tickers_map = {t["symbol"]: t for t in all_tickers}

        # Pre-filter by volume
        MIN_VOLUME_USDT = 100_000
        candidates = []
        for symbol in symbols:
            ticker = tickers_map.get(symbol)
            if not ticker:
                continue
            volume = _safe_float(ticker.get("usdtVolume") or 0)
            price = _safe_float(ticker.get("lastPr") or 0)
            if price > 0 and volume >= MIN_VOLUME_USDT:
                candidates.append((symbol, ticker))

        logger.info(
            "Bitget pre-filter: %d → %d candidates (volume >= $%s)",
            len(symbols), len(candidates), f"{MIN_VOLUME_USDT:,.0f}",
        )

        # Fetch OI for candidates
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i + 10]
            tasks = [
                self._get_single_market_data(symbol, ticker)
                for symbol, ticker in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (symbol, _), data in zip(batch, batch_results):
                if isinstance(data, BitgetMarketData):
                    result[symbol] = data

            if i + 10 < len(candidates):
                await asyncio.sleep(0.5)

        return result

    async def _get_single_market_data(self, symbol: str, ticker: Dict) -> BitgetMarketData:
        try:
            # Check cache
            cached = self._symbol_cache.get(symbol)
            if cached is not None:
                cached.timestamp = datetime.utcnow()
                cached.price = _safe_float(ticker.get("lastPr"))
                cached.volume_24h = _safe_float(ticker.get("usdtVolume"))
                return cached

            oi_hist = await self.get_open_interest(symbol)

            current_oi = 0.0
            oi_trend = "flat"
            if oi_hist:
                current_oi = _safe_float(oi_hist[-1].get("openInterest"))
            if len(oi_hist) >= 2:
                v0 = _safe_float(oi_hist[0].get("openInterest"))
                v1 = _safe_float(oi_hist[-1].get("openInterest"))
                if v1 > v0 * 1.005:
                    oi_trend = "growing"
                elif v1 < v0 * 0.995:
                    oi_trend = "shrinking"

            # Price change
            last_price = _safe_float(ticker.get("lastPr"))
            open_price = _safe_float(ticker.get("open24h") or last_price)
            price_change_24h = (
                ((last_price - open_price) / open_price * 100)
                if open_price > 0 else 0.0
            )

            result = BitgetMarketData(
                symbol=symbol,
                price=last_price,
                volume_24h=_safe_float(ticker.get("usdtVolume")),
                open_interest=current_oi,
                funding_rate=_safe_float(ticker.get("fundingRate")),
                long_short_ratio=None,  # Not available on public API
                price_change_24h=price_change_24h,
                timestamp=datetime.utcnow(),
                oi_trend=oi_trend,
                top_trader_long_short_ratio=None,
                taker_buy_sell_ratio=None,
                recent_liquidations_usd=None,
                liq_side=None,
            )
            self._symbol_cache.set(symbol, result)
            return result
        except Exception as e:
            logger.error("Error fetching Bitget %s: %s", symbol, e)
            raise
