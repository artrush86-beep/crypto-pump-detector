"""Automatic proxy health checker.

Runs in background, checks configured proxies every N minutes:
  1. Tests each proxy against exchange APIs
  2. Marks dead/geo-blocked proxies in cooldown
  3. Fetches new free proxies if pool is too small
  4. Logs status for monitoring

Usage (in main.py):
    checker = ProxyHealthChecker()
    await checker.start()
    # ... later ...
    await checker.stop()
"""

import asyncio
import aiohttp
import logging
import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass

from src.exchanges.proxy_session import (
    mark_proxy_failure,
    mark_proxy_success,
    add_dynamic_proxy,
    get_all_proxies,
    get_proxy_pool_stats,
    mask_proxy,
)

logger = logging.getLogger(__name__)


# ── Test endpoints (lightweight, public) ────────────────────────────────

TEST_ENDPOINTS = {
    "binance": {
        "url": "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "params": {"symbol": "BTCUSDT"},
    },
    "bybit": {
        "url": "https://api.bybit.com/v5/market/tickers",
        "params": {"category": "linear", "symbol": "BTCUSDT"},
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/market/ticker",
        "params": {"instId": "BTC-USDT-SWAP"},
    },
    "bitget": {
        "url": "https://api.bitget.com/api/v2/mix/market/tickers",
        "params": {"productType": "USDT-FUTURES"},
    },
    "gateio": {
        "url": "https://api.gateio.ws/api/v4/futures/usdt/tickers",
        "params": {},
    },
    "mexc": {
        "url": "https://contract.mexc.com/api/v1/contract/ticker",
        "params": {},
    },
}


@dataclass
class ProxyCheckResult:
    proxy: str
    exchange: str
    status: str  # "ok", "geo_blocked", "auth_required", "dead", "timeout"
    response_time_ms: int = 0
    error: str = ""


class ProxyHealthChecker:
    """Background proxy health checker with auto-fetch.

    Config:
        CHECK_INTERVAL: seconds between full checks (default 600 = 10 min)
        PROXY_TEST_TIMEOUT: seconds per proxy test (default 8)
        MIN_PROXIES: minimum proxies per exchange before fetching (default 2)
        FETCH_ENABLED: whether to fetch free proxies (default True)
    """

    CHECK_INTERVAL = 600       # 10 minutes
    PROXY_TEST_TIMEOUT = 8     # seconds
    MIN_PROXIES = 2            # fetch new if below this
    FETCH_ENABLED = True
    FREE_PROXY_URL = (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies"
        "&proxy_format=protocolipport"
        "&format=text"
        "&protocol=http"
        "&timeout=5000"
    )

    def __init__(self):
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_check: float = 0
        self._last_fetch: float = 0
        self._stats = {
            "checks_run": 0,
            "proxies_tested": 0,
            "proxies_removed": 0,
            "proxies_added": 0,
            "last_check_time": None,
            "last_check_duration": 0,
        }

    async def start(self):
        """Start the background health checker."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Proxy health checker started (interval: %ds)", self.CHECK_INTERVAL)

    async def stop(self):
        """Stop the health checker."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Proxy health checker stopped")

    async def _run_loop(self):
        """Main checker loop."""
        while self._running:
            try:
                # Wait before first check
                await asyncio.sleep(60)  # 1 min initial delay

                while self._running:
                    try:
                        await self.run_check()
                    except Exception as e:
                        logger.error("Proxy health check failed: %s", e)

                    # Sleep with interrupt check
                    await asyncio.wait_for(
                        asyncio.sleep(self.CHECK_INTERVAL),
                        timeout=self.CHECK_INTERVAL,
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Proxy checker loop error: %s", e)
                await asyncio.sleep(60)

    async def run_check(self):
        """Run a full proxy health check."""
        check_start = time.time()
        self._stats["checks_run"] += 1

        # Collect all unique proxies across exchanges
        all_checks: Dict[str, List[str]] = {}
        for exchange in ("binance", "bybit", "okx"):
            proxies = get_all_proxies(exchange)
            if proxies:
                all_checks[exchange] = proxies

        if not all_checks:
            logger.debug("No proxies configured, skipping health check")
            return

        total_proxies = sum(len(v) for v in all_checks.values())
        logger.info(
            "Proxy health check: testing %d proxies across %d exchanges",
            total_proxies, len(all_checks),
        )

        # Test all proxies
        results: List[ProxyCheckResult] = []
        async with aiohttp.ClientSession() as session:
            for exchange, proxies in all_checks.items():
                # Test each proxy against its exchange
                batch_tasks = [
                    self._test_proxy(session, proxy, exchange)
                    for proxy in proxies
                ]
                batch_results = await asyncio.gather(*batch_tasks, return_exceptions=True)
                for result in batch_results:
                    if isinstance(result, ProxyCheckResult):
                        results.append(result)

        self._stats["proxies_tested"] = len(results)

        # Process results
        alive = [r for r in results if r.status == "ok"]
        dead = [r for r in results if r.status in ("dead", "timeout", "auth_required")]
        geo_blocked = [r for r in results if r.status == "geo_blocked"]

        # Mark dead proxies in cooldown
        for r in dead:
            mark_proxy_failure(r.exchange, r.proxy)
            self._stats["proxies_removed"] += 1

        for r in geo_blocked:
            mark_proxy_failure(r.exchange, r.proxy)
            self._stats["proxies_removed"] += 1

        # Log alive proxies (clear cooldown for working ones)
        for r in alive:
            mark_proxy_success(r.exchange, r.proxy)

        # Fetch new proxies if pool is too small
        if self.FETCH_ENABLED:
            await self._maybe_fetch_proxies(all_checks)

        # Log summary
        duration = time.time() - check_start
        self._stats["last_check_time"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        self._stats["last_check_duration"] = round(duration, 1)
        self._last_check = time.time()

        pool_stats = get_proxy_pool_stats()
        active_total = sum(s["active"] for s in pool_stats.values())

        logger.info(
            "Proxy check done in %.1fs: %d tested, %d alive, %d dead, %d geo-blocked. "
            "Active pool: %d proxies",
            duration, len(results), len(alive), len(dead), len(geo_blocked),
            active_total,
        )

    async def _test_proxy(
        self,
        session: aiohttp.ClientSession,
        proxy: str,
        exchange: str,
    ) -> ProxyCheckResult:
        """Test a single proxy against an exchange endpoint."""
        ep = TEST_ENDPOINTS.get(exchange)
        if not ep:
            return ProxyCheckResult(proxy=proxy, exchange=exchange, status="dead", error="Unknown exchange")

        start = time.monotonic()
        kwargs = {"timeout": aiohttp.ClientTimeout(total=self.PROXY_TEST_TIMEOUT)}
        if proxy:
            kwargs["proxy"] = proxy

        try:
            async with session.get(ep["url"], params=ep.get("params", {}), **kwargs) as resp:
                elapsed = int((time.monotonic() - start) * 1000)

                if resp.status == 200:
                    return ProxyCheckResult(proxy=proxy, exchange=exchange, status="ok", response_time_ms=elapsed)
                elif resp.status in (451, 403):
                    return ProxyCheckResult(proxy=proxy, exchange=exchange, status="geo_blocked",
                                            response_time_ms=elapsed, error=f"HTTP {resp.status}")
                elif resp.status == 407:
                    return ProxyCheckResult(proxy=proxy, exchange=exchange, status="auth_required",
                                            response_time_ms=elapsed, error="407 Proxy Auth Required")
                elif resp.status == 429:
                    return ProxyCheckResult(proxy=proxy, exchange=exchange, status="ok",
                                            response_time_ms=elapsed, error="Rate limited (proxy works)")
                else:
                    return ProxyCheckResult(proxy=proxy, exchange=exchange, status="dead",
                                            response_time_ms=elapsed, error=f"HTTP {resp.status}")
        except (aiohttp.ClientProxyConnectionError, aiohttp.ClientConnectorError):
            return ProxyCheckResult(proxy=proxy, exchange=exchange, status="dead", error="Connection refused")
        except asyncio.TimeoutError:
            return ProxyCheckResult(proxy=proxy, exchange=exchange, status="timeout", error="Timed out")
        except Exception as e:
            return ProxyCheckResult(proxy=proxy, exchange=exchange, status="dead", error=str(e)[:100])

    async def _maybe_fetch_proxies(self, current_proxies: Dict[str, List[str]]):
        """Fetch free proxies if pool is too small."""
        # Only fetch once per hour
        if time.time() - self._last_fetch < 3600:
            return

        # Check if any exchange needs more proxies
        needs_proxies = False
        for exchange in ("binance",):  # Only Binance needs proxies
            pool = current_proxies.get(exchange, [])
            if len(pool) < self.MIN_PROXIES:
                needs_proxies = True
                break

        if not needs_proxies:
            return

        logger.info("Proxy pool too small, fetching free proxies...")
        self._last_fetch = time.time()

        try:
            free_proxies = await self._fetch_free_proxies()
            if not free_proxies:
                logger.warning("No free proxies available")
                return

            # Test and add working ones
            added = 0
            async with aiohttp.ClientSession() as session:
                for proxy in free_proxies[:20]:  # Test max 20
                    result = await self._test_proxy(session, proxy, "binance")
                    if result.status == "ok":
                        if add_dynamic_proxy("binance", proxy):
                            added += 1
                            self._stats["proxies_added"] += 1
                            logger.info(
                                "Free proxy added: %s (%dms) [binance]",
                                mask_proxy(proxy), result.response_time_ms,
                            )
                    if added >= 5:  # Max 5 new proxies per fetch
                        break

            logger.info("Fetched %d working free proxies (added %d to pool)", len(free_proxies), added)

        except Exception as e:
            logger.error("Failed to fetch free proxies: %s", e)

    async def _fetch_free_proxies(self) -> List[str]:
        """Fetch fresh free HTTP proxies from public API."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    self.FREE_PROXY_URL,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        proxies = []
                        for line in text.strip().splitlines():
                            line = line.strip()
                            if line and line.startswith("http"):
                                proxies.append(line)
                        return proxies[:50]
        except Exception as e:
            logger.debug("Free proxy fetch failed: %s", e)
        return []

    def get_stats(self) -> dict:
        """Get checker statistics."""
        return dict(self._stats)

    def get_pool_stats(self) -> dict:
        """Get current proxy pool statistics."""
        return get_proxy_pool_stats()
