"""MEXC USDT-M Futures API Client with proxy support.

Symbol convention:
  MEXC uses  BTC_USDT
  Internally we use BTCUSDT  (same as Binance/Bybit)
  Conversion helpers: to_mexc_id() / from_mexc_id()

MEXC ticker endpoint includes holdVol (open interest in contracts)
and funding rate — no per-symbol enrichment needed.
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


def to_mexc_id(symbol: str) -> str:
    """BTCUSDT → BTC_USDT"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}_USDT"
    return symbol.replace("-", "_")


def from_mexc_id(mexc_symbol: str) -> str:
    """BTC_USDT → BTCUSDT"""
    return mexc_symbol.replace("_", "").replace("-", "")


@dataclass
class MEXCMarketData:
    """Market data from MEXC."""
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
    if val is None or val == '':
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class MEXCClient:
    """MEXC Contract API client with proxy support."""

    BASE_URL = "https://contract.mexc.com"
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
            "mexc",
            max_candidates=3,
            include_direct_fallback=True,
            direct_first=True,
        ):
            kwargs: Dict[str, Any] = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("MEXC request via proxy %s", mask_proxy(proxy))
            else:
                logger.debug("MEXC request via direct connection")

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("MEXC rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("Rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("mexc", proxy)
                        raise aiohttp.ClientError("IP blocked by MEXC (403)")

                    response.raise_for_status()
                    data = await response.json()

                    if not data.get("success", True):
                        raise aiohttp.ClientError(
                            f"MEXC API error: {data.get('message', '')}"
                        )

                    mark_proxy_success("mexc", proxy)
                    return data.get("data", [])

            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("mexc", proxy)
                last_error = exc
                logger.warning("MEXC route failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for MEXC request")

    async def get_all_symbols(self) -> List[str]:
        """Return active USDT perpetual symbols in BTCUSDT format."""
        data = await self._request("/api/v1/contract/ticker")
        symbols = []
        if isinstance(data, list):
            for item in data:
                sym = item.get("symbol", "")
                if sym.endswith("_USDT"):
                    symbols.append(from_mexc_id(sym))
        return symbols[:300]

    async def get_tickers(self) -> List[Dict]:
        """Batch tickers — includes holdVol (OI) and fundingRate."""
        return await self._request("/api/v1/contract/ticker")

    async def get_market_data_batch(self, symbols: List[str]) -> Dict[str, MEXCMarketData]:
        """MEXC ticker endpoint already includes OI and funding rate.

        No per-symbol enrichment needed — tickers are self-sufficient.
        """
        result = {}

        try:
            all_tickers = await self.get_tickers()
        except Exception as e:
            logger.error("MEXC: failed to fetch tickers: %s", e)
            return result

        tickers_map = {}
        if isinstance(all_tickers, list):
            for t in all_tickers:
                sym = t.get("symbol", "")
                if sym.endswith("_USDT"):
                    tickers_map[from_mexc_id(sym)] = t

        # Pre-filter by volume
        MIN_VOLUME_USDT = 100_000
        candidates = []
        for symbol in symbols:
            ticker = tickers_map.get(symbol)
            if not ticker:
                continue
            # volume24 = contract count, amount24 = USDT value
            volume = _safe_float(ticker.get("amount24") or 0)
            price = _safe_float(ticker.get("lastPrice") or 0)
            if price > 0 and volume >= MIN_VOLUME_USDT:
                candidates.append((symbol, ticker))

        logger.info(
            "MEXC pre-filter: %d → %d candidates (volume >= $%s)",
            len(symbols), len(candidates), f"{MIN_VOLUME_USDT:,.0f}",
        )

        # MEXC tickers have all needed data — no per-symbol calls needed
        for symbol, ticker in candidates:
            data = self._parse_ticker(symbol, ticker)
            if data:
                result[symbol] = data

        return result

    def _parse_ticker(self, symbol: str, ticker: Dict) -> Optional[MEXCMarketData]:
        """Parse MEXC ticker into MarketData — no async calls needed."""
        try:
            # Check cache
            cached = self._symbol_cache.get(symbol)
            if cached is not None:
                cached.timestamp = datetime.utcnow()
                cached.price = _safe_float(ticker.get("lastPrice"))
                cached.volume_24h = _safe_float(ticker.get("amount24"))
                return cached

            last_price = _safe_float(ticker.get("lastPrice"))
            # holdVol = total open interest in contracts (not USD)
            # To get USD value: holdVol * lastPrice * quantoMultiplier
            # But without knowing quantoMultiplier, use lastPrice as proxy
            hold_vol = _safe_float(ticker.get("holdVol"))
            # Approximate OI in USD — for pump detection, relative change matters more
            oi_usd = hold_vol * last_price  # rough estimate

            result = MEXCMarketData(
                symbol=symbol,
                price=last_price,
                volume_24h=_safe_float(ticker.get("amount24")),
                open_interest=oi_usd,
                funding_rate=_safe_float(ticker.get("fundingRate")),
                long_short_ratio=None,  # Not available on public API
                price_change_24h=_safe_float(ticker.get("riseFallRate")) * 100,
                timestamp=datetime.utcnow(),
                oi_trend=None,  # Would need kline history
                top_trader_long_short_ratio=None,
                taker_buy_sell_ratio=None,
                recent_liquidations_usd=None,
                liq_side=None,
            )
            self._symbol_cache.set(symbol, result)
            return result
        except Exception as e:
            logger.error("Error parsing MEXC %s: %s", symbol, e)
            return None
