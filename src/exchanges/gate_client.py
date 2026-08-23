"""Gate.io USDT-M Futures API Client with proxy support.

Symbol convention:
  Gate.io uses  BTC_USDT
  Internally we use BTCUSDT  (same as Binance/Bybit)
  Conversion helpers: to_gate_id() / from_gate_id()

Gate.io has the richest free public API:
  - contract_stats: OI + L/S ratio + liquidations + top-trader ratio (ALL in 1 call)
  - funding_rate: current funding rate
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


def to_gate_id(symbol: str) -> str:
    """BTCUSDT → BTC_USDT"""
    if symbol.endswith("USDT"):
        base = symbol[:-4]
        return f"{base}_USDT"
    return symbol.replace("-", "_")


def from_gate_id(contract: str) -> str:
    """BTC_USDT → BTCUSDT"""
    return contract.replace("_", "").replace("-", "")


@dataclass
class GateMarketData:
    """Market data from Gate.io."""
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


class GateClient:
    """Gate.io V4 API client with proxy support."""

    BASE_URL = "https://api.gateio.ws"
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
            "gateio",
            max_candidates=3,
            include_direct_fallback=True,
            direct_first=True,
        ):
            kwargs: Dict[str, Any] = {"params": params or {}, "timeout": timeout}
            if proxy:
                kwargs["proxy"] = proxy
                logger.debug("Gate.io request via proxy %s", mask_proxy(proxy))
            else:
                logger.debug("Gate.io request via direct connection")

            try:
                async with self.session.get(url, **kwargs) as response:
                    if response.status == 429:
                        logger.warning("Gate.io rate limit hit, backing off...")
                        await asyncio.sleep(1)
                        raise aiohttp.ClientError("Rate limited")
                    if response.status == 403:
                        if proxy:
                            mark_proxy_failure("gateio", proxy)
                        raise aiohttp.ClientError("IP blocked by Gate.io (403)")

                    response.raise_for_status()
                    data = await response.json()
                    mark_proxy_success("gateio", proxy)
                    return data

            except (
                aiohttp.ClientHttpProxyError,
                aiohttp.ClientProxyConnectionError,
                aiohttp.ClientConnectorError,
            ) as exc:
                if proxy:
                    mark_proxy_failure("gateio", proxy)
                last_error = exc
                logger.warning("Gate.io route failed via %s: %s", mask_proxy(proxy), exc)
                continue

        if last_error:
            raise last_error
        raise aiohttp.ClientError("No available route for Gate.io request")

    async def get_all_symbols(self) -> List[str]:
        """Return active USDT perpetual symbols in BTCUSDT format."""
        data = await self._request("/api/v4/futures/usdt/contracts")
        symbols = []
        for item in data:
            name = item.get("name", "")
            if name.endswith("_USDT") and item.get("quanto_multiplier"):
                symbols.append(from_gate_id(name))
        return symbols[:300]

    async def get_tickers(self) -> List[Dict]:
        """Batch tickers for all USDT contracts."""
        return await self._request("/api/v4/futures/usdt/tickers")

    async def get_contract_stats(self, contract: str) -> Optional[Dict]:
        """Get OI + L/S + liquidations for one contract (1 API call!).

        This is Gate.io's killer feature — one call gives:
        - open_interest: total OI in contracts
        - lsr_taker: taker buy/sell ratio
        - lsr_account: account long/short ratio
        - top_lsr_size: top trader L/S ratio by size
        - top_lsr_account: top trader L/S ratio by account
        - long_liq_size / short_liq_size: liquidation volumes
        - long_liq_usd / short_liq_usd: liquidation USD values
        """
        try:
            data = await self._request(
                "/api/v4/futures/usdt/contract_stats",
                {"contract": contract, "limit": "2"},
            )
            if data and len(data) >= 1:
                return data[-1]  # Most recent
            return None
        except Exception as e:
            logger.debug("Gate.io contract_stats not available for %s: %s", contract, e)
            return None

    async def get_funding_rate(self, contract: str) -> Optional[float]:
        """Current funding rate."""
        try:
            data = await self._request(
                "/api/v4/futures/usdt/funding_rate",
                {"contract": contract},
            )
            if data and len(data) > 0:
                return float(data[0].get("r", 0))
        except Exception as e:
            logger.debug("Gate.io funding rate not available for %s: %s", contract, e)
        return 0.0

    async def get_market_data_batch(self, symbols: List[str]) -> Dict[str, GateMarketData]:
        """Two-step fetch with Gate.io's rich contract_stats."""
        result = {}

        # Batch tickers
        try:
            all_tickers = await self.get_tickers()
        except Exception as e:
            logger.error("Gate.io: failed to fetch tickers: %s", e)
            return result

        tickers_map = {from_gate_id(t["contract"]): t for t in all_tickers if t.get("contract", "").endswith("_USDT")}

        # Pre-filter
        MIN_VOLUME_USDT = 100_000
        candidates = []
        for symbol in symbols:
            ticker = tickers_map.get(symbol)
            if not ticker:
                continue
            volume = _safe_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle") or 0)
            price = _safe_float(ticker.get("last") or 0)
            if price > 0 and volume >= MIN_VOLUME_USDT:
                candidates.append((symbol, ticker))

        logger.info(
            "Gate.io pre-filter: %d → %d candidates (volume >= $%s)",
            len(symbols), len(candidates), f"{MIN_VOLUME_USDT:,.0f}",
        )

        # Fetch contract_stats for candidates (gives OI + L/S + liqs in 1 call!)
        for i in range(0, len(candidates), 10):
            batch = candidates[i:i + 10]
            tasks = [
                self._get_single_market_data(symbol, ticker)
                for symbol, ticker in batch
            ]

            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            for (symbol, _), data in zip(batch, batch_results):
                if isinstance(data, GateMarketData):
                    result[symbol] = data

            if i + 10 < len(candidates):
                await asyncio.sleep(0.5)

        return result

    async def _get_single_market_data(self, symbol: str, ticker: Dict) -> GateMarketData:
        gate_id = to_gate_id(symbol)

        try:
            # Check cache
            cached = self._symbol_cache.get(symbol)
            if cached is not None:
                cached.timestamp = datetime.utcnow()
                cached.price = _safe_float(ticker.get("last"))
                cached.volume_24h = _safe_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle"))
                return cached

            # Get contract_stats (OI + L/S + liquidations in ONE call)
            stats = await self.get_contract_stats(gate_id)

            # Price change from ticker
            last_price = _safe_float(ticker.get("last"))
            change_pct = _safe_float(ticker.get("change_percentage"))

            # Volume from ticker (quote = USDT)
            volume_24h = _safe_float(ticker.get("volume_24h_quote") or ticker.get("volume_24h_settle") or 0)

            # Parse stats
            current_oi = 0.0
            oi_trend = "flat"
            long_ratio = None
            top_trader_ratio = None
            taker_ratio = None
            liq_usd = 0.0
            liq_side = None

            if stats:
                current_oi = _safe_float(stats.get("open_interest_usd"))  # USD value
                if current_oi == 0:
                    # Fallback: contract-size OI
                    current_oi = _safe_float(stats.get("open_interest")) * last_price

                # Account L/S ratio (>1 = more longs)
                lsr_account = _safe_float(stats.get("lsr_account"))
                if lsr_account > 0:
                    # Convert to fraction (0-1)
                    long_ratio = lsr_account / (lsr_account + 1)

                # Top trader ratios
                top_lsr = _safe_float(stats.get("top_lsr_size"))
                if top_lsr > 0:
                    top_trader_ratio = top_lsr / (top_lsr + 1)

                taker_lsr = _safe_float(stats.get("lsr_taker"))
                if taker_lsr > 0:
                    taker_ratio = taker_lsr / (taker_lsr + 1)

                # Liquidations
                long_liq = _safe_float(stats.get("long_liq_usd_new") or stats.get("long_liq_usd"))
                short_liq = _safe_float(stats.get("short_liq_usd_new") or stats.get("short_liq_usd"))
                liq_usd = long_liq + short_liq
                if liq_usd > 0:
                    liq_side = "long" if long_liq > short_liq else "short"

            result = GateMarketData(
                symbol=symbol,
                price=last_price,
                volume_24h=volume_24h,
                open_interest=current_oi,
                funding_rate=_safe_float(ticker.get("funding_rate")),
                long_short_ratio=long_ratio,
                price_change_24h=change_pct,
                timestamp=datetime.utcnow(),
                oi_trend=oi_trend,
                top_trader_long_short_ratio=top_trader_ratio,
                taker_buy_sell_ratio=taker_ratio,
                recent_liquidations_usd=liq_usd if liq_usd > 0 else None,
                liq_side=liq_side,
            )
            self._symbol_cache.set(symbol, result)
            return result

        except Exception as e:
            logger.error("Error fetching Gate.io %s: %s", symbol, e)
            raise
