"""Binance Futures API Client with proxy support for Railway deployment."""

import aiohttp
import asyncio
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
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


def is_fatal_error(exc):
    """Give up immediately only for truly unrecoverable errors.
    NOTE: 451 removed — handled inside proxy loop (try next proxy).
    NOTE: 403 removed — also handled inside proxy loop.
    """
    if isinstance(exc, aiohttp.ClientResponseError):
        # 400: Bad Request (wrong params/symbol — no point retrying)
        # 404: Not Found (symbol has no data — no point retrying)
        return exc.status in (400, 404)
    return False


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
    # NEW fields — default None so existing code won't break
    top_trader_long_short_ratio: Optional[float] = None   # Top traders (smarter money)
    taker_buy_sell_ratio: Optional[float] = None         # 0–1: >0.6 = aggressive buying
    recent_liquidations_usd: Optional[float] = None      # USD liquidated last 15m
    liq_side: Optional[str] = None                       # 'SHORT' or 'LONG' dominated
    oi_trend: Optional[str] = None                       # 'growing'|'shrinking'|'flat'


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
    
    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.TimeoutError),
        max_tries=3,
        giveup=is_fatal_error
    )
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
                        logger.error("Binance 403 Forbidden - IP may be blocked.")
                        raise aiohttp.ClientError("IP blocked by Binance (403)")
                    if response.status == 451:
                        # 451 = geo-blocked (US proxy IPs banned by Binance)
                        # FIX: try next proxy instead of immediate giveup via is_fatal_error
                        if proxy:
                            mark_proxy_failure("binance", proxy)
                            logger.warning("Binance 451 geo-block on %s, trying next proxy", mask_proxy(proxy))
                        else:
                            logger.error("Binance 451 on direct connection — need non-US proxies")
                        last_error = aiohttp.ClientError("Geo-blocked by Binance (451)")
                        continue

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
    
    # ------------------------------------------------------------------ #
    #  Original endpoints (unchanged)                                       #
    # ------------------------------------------------------------------ #

    async def get_all_symbols(self) -> List[str]:
        data = await self._request("/fapi/v1/exchangeInfo")
        symbols = [
            s['symbol'] for s in data['symbols']
            if s['status'] == 'TRADING' and s['contractType'] == 'PERPETUAL'
            and s['symbol'].endswith('USDT')
        ]
        return symbols[:200]
    
    async def get_all_tickers(self) -> List[Dict]:
        return await self._request("/fapi/v1/ticker/24hr")
    
    async def get_open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 2) -> List[Dict]:
        return await self._request("/futures/data/openInterestHist", {
            "symbol": symbol, "period": period, "limit": limit
        })
    
    async def get_funding_rate(self, symbol: str) -> Dict:
        return await self._request("/fapi/v1/premiumIndex", {"symbol": symbol})
    
    async def get_long_short_ratio(self, symbol: str, period: str = "15m") -> List[Dict]:
        """Global account long/short ratio (all traders)."""
        return await self._request("/futures/data/globalLongShortAccountRatio", {
            "symbol": symbol, "period": period, "limit": 2
        })

    # ------------------------------------------------------------------ #
    #  NEW: Top trader long/short ratio                                     #
    # ------------------------------------------------------------------ #

    async def get_top_trader_ls_ratio(self, symbol: str, period: str = "15m") -> Optional[float]:
        """
        Top trader POSITION long/short ratio — smarter money than global ratio.

        Endpoint: GET /futures/data/topLongShortPositionRatio
        FREE, public, no auth needed.

        WHY IT'S BETTER THAN globalLongShortAccountRatio:
        - Top traders hold the largest positions on Binance
        - When top traders go long while retail is short → strong buy signal
        - Use alongside global ratio for divergence detection

        HOW TO USE IN SCORER:
            top_ls = await client.get_top_trader_ls_ratio(symbol)
            global_ls = signal.long_short_ratio   # existing field

            if top_ls and global_ls:
                if top_ls > 1.5 and global_ls < 1.0:
                    # Top traders long, retail short → PUMP fuel
                    factors.append('Top traders long vs retail short')
                    score += 1.0
                elif top_ls < 0.7 and global_ls > 1.2:
                    # Top traders short, retail long → DUMP fuel
                    factors.append('Top traders short vs retail long')
                    score += 1.0  # (for dump signal)

        Returns: ratio value (>1 = more longs), or None on error.
        """
        try:
            data = await self._request("/futures/data/topLongShortPositionRatio", {
                "symbol": symbol, "period": period, "limit": 1
            })
            if data and len(data) > 0:
                return float(data[-1].get('longShortRatio', 1.0))
        except Exception as e:
            logger.debug(f"Top trader L/S not available for {symbol}: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  NEW: Taker buy/sell volume ratio                                     #
    # ------------------------------------------------------------------ #

    async def get_taker_buy_ratio(self, symbol: str, period: str = "15m") -> Optional[float]:
        """
        Taker buy volume as fraction of total volume (0.0–1.0).

        Endpoint: GET /futures/data/takerbuybaseAssetVol
        FREE, public, no auth needed.

        WHAT IT MEANS:
        - Taker = market order = aggressive / conviction trade
        - taker_buy_ratio > 0.6 → buyers are aggressive = bullish pressure
        - taker_buy_ratio < 0.4 → sellers are aggressive = bearish pressure
        - Combined with OI increase: OI up + taker_buy > 0.6 = STRONG pump signal

        HOW TO USE IN SCORER:
            tbr = await client.get_taker_buy_ratio(symbol)
            if tbr is not None:
                if tbr > 0.65 and signal.is_pump:
                    factors.append(f'Aggressive buyers {tbr:.0%} of volume')
                    score += 1.0
                elif tbr < 0.35 and not signal.is_pump:
                    factors.append(f'Aggressive sellers {(1-tbr):.0%} of volume')
                    score += 1.0

        Returns: buy ratio 0.0–1.0, or None on error.
        """
        try:
            # Get buy volume
            buy_data = await self._request("/futures/data/takerbuybaseAssetVol", {
                "symbol": symbol, "period": period, "limit": 1
            })
            # Get total volume from tickers (already fetched in batch)
            # Fall back to computing ratio from buy vs sell
            sell_data = await self._request("/futures/data/takersellbaseAssetVol", {
                "symbol": symbol, "period": period, "limit": 1
            })
            if buy_data and sell_data:
                buy_vol = float(buy_data[-1].get('buySellRatio', buy_data[-1].get('buyVol', 0)))
                # If endpoint returns buySellRatio directly, use it
                if 'buySellRatio' in buy_data[-1]:
                    ratio = buy_vol / (buy_vol + 1)  # convert ratio to fraction
                    return min(max(ratio, 0.0), 1.0)
                # Otherwise compute from buy + sell volumes
                sell_vol = float(sell_data[-1].get('sellVol', 0))
                total = buy_vol + sell_vol
                if total > 0:
                    return buy_vol / total
        except Exception as e:
            logger.debug(f"Taker buy ratio not available for {symbol}: {e}")
        return None

    async def get_taker_buy_sell_ratio(self, symbol: str, period: str = "15m") -> Optional[float]:
        """
        Simpler version: uses /futures/data/takerBuySellRatio directly.
        Returns buy/sell ratio (>1.0 = more buying, <1.0 = more selling).

        HOW TO USE IN SCORER:
            ratio = await client.get_taker_buy_sell_ratio(symbol)
            if ratio:
                if ratio > 1.5:
                    score += 1.0  # Strong buying pressure
                    factors.append(f'Taker buy/sell ratio: {ratio:.2f}')
                elif ratio < 0.67:
                    score += 1.0  # Strong selling pressure (for dump)
        """
        try:
            data = await self._request("/futures/data/takerBuySellRatio", {
                "symbol": symbol, "period": period, "limit": 1
            })
            if data and len(data) > 0:
                return float(data[-1].get('buySellRatio', 1.0))
        except Exception as e:
            logger.debug(f"Taker buy/sell ratio not available for {symbol}: {e}")
        return None

    # ------------------------------------------------------------------ #
    #  NEW: Recent liquidations (PUBLIC endpoint — no auth needed)          #
    # ------------------------------------------------------------------ #

    async def get_recent_liquidations(self, symbol: str, limit: int = 10) -> List[Dict]:
        """
        Get most recent forced liquidation orders for a symbol.

        Endpoint: GET /fapi/v1/allForceOrders
        PUBLIC endpoint — no API key needed, completely free.

        WHAT IT RETURNS per liquidation:
            symbol, side (BUY=short liq, SELL=long liq),
            price, origQty, executedQty, averagePrice, status, time

        HOW TO USE IN SCORER:
            liqs = await client.get_recent_liquidations(symbol, limit=20)
            liq_usd, liq_side = client.analyze_liquidations(liqs)

            if liq_usd > 500_000:  # >$500k liquidated recently
                if liq_side == 'SHORT' and signal.is_pump:
                    factors.append(f'Short squeeze: ${liq_usd/1e6:.1f}M liquidated')
                    score += 1.5   # Shorts being wiped = forced buying = pump accelerates
                elif liq_side == 'LONG' and not signal.is_pump:
                    factors.append(f'Long liquidations: ${liq_usd/1e6:.1f}M')
                    score += 1.5   # Longs being wiped = forced selling = dump accelerates
        """
        try:
            return await self._request("/fapi/v1/allForceOrders", {
                "symbol": symbol, "limit": limit
            })
        except Exception as e:
            logger.debug(f"Liquidations not available for {symbol}: {e}")
            return []

    def analyze_liquidations(self, liquidations: List[Dict]) -> tuple:
        """
        Summarize liquidation list into (total_usd, dominant_side).

        dominant_side:
            'SHORT' = short liquidations dominated (BUY side = short squeeze)
            'LONG'  = long liquidations dominated (SELL side = long wipeout)
            'MIXED' = roughly equal

        Returns: (total_usd_liquidated: float, side: str)
        """
        if not liquidations:
            return 0.0, 'NONE'

        short_liq_usd = 0.0  # BUY orders = short positions liquidated
        long_liq_usd = 0.0   # SELL orders = long positions liquidated

        for liq in liquidations:
            qty = float(liq.get('executedQty') or liq.get('origQty') or 0)
            price = float(liq.get('averagePrice') or liq.get('price') or 0)
            usd_value = qty * price
            if liq.get('side') == 'BUY':
                short_liq_usd += usd_value
            elif liq.get('side') == 'SELL':
                long_liq_usd += usd_value

        total = short_liq_usd + long_liq_usd
        if total == 0:
            return 0.0, 'NONE'

        if short_liq_usd > long_liq_usd * 1.5:
            side = 'SHORT'
        elif long_liq_usd > short_liq_usd * 1.5:
            side = 'LONG'
        else:
            side = 'MIXED'

        return total, side

    # ------------------------------------------------------------------ #
    #  NEW: OI trend over multiple periods                                  #
    # ------------------------------------------------------------------ #

    async def get_oi_trend(self, symbol: str, periods: int = 4) -> str:
        """
        Analyze OI direction over last N 15m candles.

        Returns: 'growing' | 'shrinking' | 'flat'

        HOW TO USE IN SCORER:
            oi_trend = await client.get_oi_trend(symbol, periods=4)
            if oi_trend == 'growing' and signal.is_pump:
                factors.append('OI consistently growing (4 periods)')
                score += 0.5  # Confirmation that new money entering
            elif oi_trend == 'shrinking' and signal.is_pump:
                score -= 0.5  # Warning: volume might be short covering, not fresh longs

        Uses only 1 extra API call vs existing get_open_interest_hist(limit=2).
        """
        try:
            oi_hist = await self.get_open_interest_hist(symbol, "15m", limit=periods + 1)
            if len(oi_hist) < 2:
                return 'flat'

            values = [float(x['sumOpenInterest']) for x in oi_hist]
            # Count how many consecutive moves are in same direction
            increases = sum(1 for i in range(1, len(values)) if values[i] > values[i-1])
            decreases = sum(1 for i in range(1, len(values)) if values[i] < values[i-1])
            total = increases + decreases

            if total == 0:
                return 'flat'
            if increases / total >= 0.75:
                return 'growing'
            if decreases / total >= 0.75:
                return 'shrinking'
            return 'flat'
        except Exception as e:
            logger.debug(f"OI trend not available for {symbol}: {e}")
            return 'flat'

    # ------------------------------------------------------------------ #
    #  Batch data fetch (enhanced)                                          #
    # ------------------------------------------------------------------ #

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
        """Fetch all data for one symbol, including new metrics."""
        try:
            # Original calls
            oi_hist = await self.get_open_interest_hist(symbol, "15m", 2)
            funding = await self.get_funding_rate(symbol)
            ls_ratio = await self.get_long_short_ratio(symbol)
            
            # Extended fields (optional, may be None)
            top_trader_long_short_ratio = await self.get_top_trader_ls_ratio(symbol)
            taker_ratio = await self.get_taker_buy_sell_ratio(symbol)
            liqs = await self.get_recent_liquidations(symbol, limit=20)
            liq_usd, liq_side = self.analyze_liquidations(liqs)

            # Safely unpack (gather returns exceptions as values)
            top_trader_long_short_ratio = top_trader_long_short_ratio if isinstance(top_trader_long_short_ratio, float) else None
            taker_ratio = taker_ratio if isinstance(taker_ratio, float) else None
            liqs = liqs if isinstance(liqs, list) else []

            # OI trend from existing hist
            oi_trend = 'flat'
            if len(oi_hist) >= 2:
                v0 = float(oi_hist[0]['sumOpenInterest'])
                v1 = float(oi_hist[-1]['sumOpenInterest'])
                if v1 > v0 * 1.005:
                    oi_trend = 'growing'
                elif v1 < v0 * 0.995:
                    oi_trend = 'shrinking'

            current_oi = float(oi_hist[-1]['sumOpenInterest']) if oi_hist else 0
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
                timestamp=datetime.utcnow(),
                # NEW fields
                top_trader_long_short_ratio=top_trader_long_short_ratio,
                taker_buy_sell_ratio=taker_ratio,
                recent_liquidations_usd=liq_usd,
                liq_side=liq_side,
                oi_trend=oi_trend,
            )
        except Exception as e:
            logger.error(f"Error fetching {symbol}: {e}")
            raise
