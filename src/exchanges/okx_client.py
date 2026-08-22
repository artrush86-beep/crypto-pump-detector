"""OKX Futures (SWAP) API Client with proxy support for Railway deployment.

Symbol convention:
  OKX uses  BTC-USDT-SWAP   (instId)
  Internally we use BTCUSDT  (same as Binance/Bybit)
  Conversion helpers: to_okx_id() / from_okx_id()
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
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
#  Symbol conversion helpers
# ────────────────────────────────────────────────────────────────────────────

def to_okx_id(symbol: str) -> str:
    """BTCUSDT  →  BTC-USDT-SWAP"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}-USDT-SWAP"
    # Fallback: just append -SWAP (handles BTCBUSD etc.)
    return f"{symbol}-SWAP"


def from_okx_id(inst_id: str) -> str:
    """BTC-USDT-SWAP  →  BTCUSDT"""
    # e.g. BTC-USDT-SWAP → BTC + USDT
    parts = inst_id.split("-")
    if len(parts) >= 2:
        return parts[0] + parts[1]
    return inst_id.replace("-", "").replace("SWAP", "")


# ────────────────────────────────────────────────────────────────────────────
#  Data class  (mirrors BybitMarketData / Binance MarketData)
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class OKXMarketData:
    symbol: str                         # internal BTCUSDT format
    price: float
    volume_24h: float
    open_interest: float
    funding_rate: float
    long_short_ratio: Optional[float]
    price_change_24h: float
    timestamp: datetime
    # Extended fields — optional, same names as Binance/Bybit for detector compatibility
    top_trader_long_short_ratio: Optional[float] = None
    taker_buy_sell_ratio: Optional[float] = None
    recent_liquidations_usd: Optional[float] = None
    liq_side: Optional[str] = None
    oi_trend: Optional[str] = None


# ────────────────────────────────────────────────────────────────────────────
#  Client
# ────────────────────────────────────────────────────────────────────────────

class OKXClient:
    """OKX V5 API client with proxy support."""

    BASE_URL = "https://www.okx.com"

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        self.session = create_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    # ── low-level request ────────────────────────────────────────────────

    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.TimeoutError),
        max_tries=3,
    )
    async def _request(self, endpoint: str, params: Dict = None) -> Any:
        """GET request to OKX public API, with proxy fallback."""
        url = f"{self.BASE_URL}{endpoint}"
        timeout = aiohttp.ClientTimeout(total=15)
        last_error: Optional[Exception] = None

        for proxy in get_proxy_candidates(
            "okx",
            max_candidates=3,
            include_direct_fallback=True,
            direct_first=True,
        ):
            kwargs: Dict[str, Any] = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("OKX request via proxy %s", mask_proxy(proxy))
            else:
                logger.debug("OKX request via direct connection")

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("OKX rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("OKX rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("okx", proxy)
                        logger.error("OKX 403 - IP blocked. Check proxy settings.")
                        raise aiohttp.ClientError("IP blocked by OKX (403)")

                    response.raise_for_status()
                    data = await response.json()

                    # OKX returns {"code": "0", "data": [...]}
                    if str(data.get("code", "0")) != "0":
                        raise aiohttp.ClientError(
                            f"OKX API error {data.get('code')}: {data.get('msg')}"
                        )

                    mark_proxy_success("okx", proxy)
                    return data.get("data", [])

            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("okx", proxy)
                last_error = exc
                logger.warning("OKX route failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for OKX request")

    # ── public endpoints ─────────────────────────────────────────────────

    async def get_all_symbols(self) -> List[str]:
        """Return list of active USDT perpetual symbols in internal BTCUSDT format."""
        data = await self._request(
            "/api/v5/public/instruments",
            {"instType": "SWAP"},
        )
        symbols = []
        for item in data:
            inst_id = item.get("instId", "")
            # Only USDT-settled perps
            if inst_id.endswith("-USDT-SWAP") and item.get("state") == "live":
                symbols.append(from_okx_id(inst_id))
        return symbols[:300]

    async def get_tickers(self) -> List[Dict]:
        """Batch ticker for all SWAP instruments."""
        return await self._request("/api/v5/market/tickers", {"instType": "SWAP"})

    async def get_open_interest(self, okx_inst_id: str) -> List[Dict]:
        """Historical OI for a single instrument (last 2 periods)."""
        try:
            return await self._request(
                "/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": okx_inst_id},
            )
        except Exception as e:
            logger.debug("OKX OI not available for %s: %s", okx_inst_id, e)
            return []

    async def get_funding_rate(self, okx_inst_id: str) -> Optional[float]:
        """Current funding rate for a single instrument."""
        try:
            data = await self._request(
                "/api/v5/public/funding-rate",
                {"instId": okx_inst_id},
            )
            if data:
                return float(data[0].get("fundingRate", 0))
        except Exception as e:
            logger.debug("OKX funding rate not available for %s: %s", okx_inst_id, e)
        return 0.0

    async def get_long_short_ratio(self, ccy: str, period: str = "5m") -> Optional[float]:
        """
        Long/short account ratio for a currency (e.g. 'BTC').
        Endpoint: /api/v5/rubik/stat/contracts/long-short-account-ratio
        Returns longRatio (0-1) or None on error.
        """
        try:
            data = await self._request(
                "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                {"ccy": ccy, "period": period},
            )
            if data:
                # Most recent entry is last
                entry = data[-1]
                ls_ratio = entry.get("longShortRatio")
                if ls_ratio is not None:
                    ratio = float(ls_ratio)
                    # Convert ratio (>1 means more longs) to longRatio fraction
                    return ratio / (ratio + 1)
        except Exception as e:
            logger.debug("OKX L/S ratio not available for %s: %s", ccy, e)
        return None

    # ── batch fetch ───────────────────────────────────────────────────────

    async def get_market_data_batch(
        self, symbols: List[str]
    ) -> Dict[str, OKXMarketData]:
        """
        Fetch market data for all symbols.
        symbols: internal format (BTCUSDT etc.)
        Returns: dict keyed by internal symbol.
        """
        result: Dict[str, OKXMarketData] = {}

        # Batch tickers in one call
        try:
            all_tickers_raw = await self.get_tickers()
        except Exception as e:
            error_detail = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.error("OKX: failed to fetch tickers: %s", error_detail)
            return result

        # Build lookup by internal symbol
        tickers_map: Dict[str, Dict] = {}
        for t in all_tickers_raw:
            inst_id = t.get("instId", "")
            if inst_id.endswith("-USDT-SWAP"):
                tickers_map[from_okx_id(inst_id)] = t

        # Per-symbol enrichment in small batches to avoid rate limits
        for i in range(0, len(symbols), 5):
            batch = symbols[i : i + 5]
            tasks = []
            valid_symbols = []
            for sym in batch:
                if sym in tickers_map:
                    valid_symbols.append(sym)
                    tasks.append(
                        self._get_single_market_data(sym, tickers_map[sym])
                    )

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for sym, res in zip(valid_symbols, batch_results):
                if isinstance(res, OKXMarketData):
                    result[sym] = res

            if i + 5 < len(symbols):
                await asyncio.sleep(0.3)

        return result

    async def _get_single_market_data(
        self, symbol: str, ticker: Dict
    ) -> OKXMarketData:
        """Fetch full data for one symbol."""
        okx_id = to_okx_id(symbol)
        ccy = symbol.replace("USDT", "")  # BTCUSDT → BTC

        try:
            # Parallel fetch of OI and funding
            oi_data, funding_rate, long_ratio = await asyncio.gather(
                self.get_open_interest(okx_id),
                self.get_funding_rate(okx_id),
                self.get_long_short_ratio(ccy),
                return_exceptions=True,
            )

            # Safe unpack
            if isinstance(oi_data, Exception):
                oi_data = []
            if isinstance(funding_rate, Exception):
                funding_rate = 0.0
            if isinstance(long_ratio, Exception):
                long_ratio = None

            # OI value and trend from 2-period history
            current_oi = 0.0
            oi_trend = "flat"
            if oi_data and len(oi_data) >= 1:
                try:
                    current_oi = float(oi_data[-1].get("oi", 0))
                except (ValueError, TypeError):
                    current_oi = 0.0
            if oi_data and len(oi_data) >= 2:
                try:
                    v0 = float(oi_data[0].get("oi", 0))
                    v1 = float(oi_data[-1].get("oi", 0))
                    if v1 > v0 * 1.005:
                        oi_trend = "growing"
                    elif v1 < v0 * 0.995:
                        oi_trend = "shrinking"
                except (ValueError, TypeError):
                    pass

            # Price and volume from ticker
            last_price = float(ticker.get("last", 0) or 0)
            open_price = float(ticker.get("open24h", last_price) or last_price)
            price_change_24h = (
                ((last_price - open_price) / open_price * 100)
                if open_price > 0
                else 0.0
            )
            # volCcy24h = base currency volume; volCcyQuote24h = quote (USDT) volume
            volume_24h = float(ticker.get("volCcy24h", ticker.get("vol24h", 0)) or 0)

            return OKXMarketData(
                symbol=symbol,
                price=last_price,
                volume_24h=volume_24h,
                open_interest=current_oi,
                funding_rate=float(funding_rate or 0),
                long_short_ratio=long_ratio,
                price_change_24h=price_change_24h,
                timestamp=datetime.utcnow(),
                oi_trend=oi_trend,
                # OKX public API doesn't expose top-trader ratio or liquidations freely
                top_trader_long_short_ratio=None,
                taker_buy_sell_ratio=None,
                recent_liquidations_usd=None,
                liq_side=None,
            )

        except Exception as e:
            logger.error("Error fetching OKX %s: %s", symbol, e)
            raise
