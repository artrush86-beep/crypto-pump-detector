"""Proxy-aware aiohttp session helpers with rotation and cooldown support."""

from __future__ import annotations

import aiohttp
import logging
import time
from collections import defaultdict
from typing import Dict, List, Optional

from config.settings import settings

logger = logging.getLogger(__name__)

_PROXY_FAILURES: Dict[str, Dict[str, float]] = defaultdict(dict)
_PROXY_INDEX: Dict[str, int] = defaultdict(int)


def create_session() -> aiohttp.ClientSession:
    """Create an aiohttp session shared by exchange clients."""
    connector = aiohttp.TCPConnector(
        limit=20,
        ttl_dns_cache=300,
        ssl=False,
    )
    return aiohttp.ClientSession(connector=connector)


def mask_proxy(proxy_url: Optional[str]) -> str:
    """Mask credentials when logging proxy URLs."""
    if not proxy_url:
        return "direct"
    return proxy_url.split("@")[-1] if "@" in proxy_url else proxy_url


def _parse_proxy_list(raw_value: Optional[str]) -> List[str]:
    """Parse comma/newline-separated proxy list preserving order."""
    if not raw_value:
        return []

    items: List[str] = []
    for chunk in raw_value.replace(";", "\n").replace(",", "\n").splitlines():
        proxy = chunk.strip()
        if proxy and proxy not in items:
            items.append(proxy)
    return items


def _configured_proxies(exchange_name: str) -> List[str]:
    """Return configured proxies for a given exchange."""
    exchange = exchange_name.lower()

    proxies: List[str] = []
    if exchange == "binance":
        proxies.extend(_parse_proxy_list(settings.BINANCE_PROXY_URLS))
        if settings.BINANCE_PROXY_URL:
            proxies.append(settings.BINANCE_PROXY_URL)
    elif exchange == "bybit":
        proxies.extend(_parse_proxy_list(settings.BYBIT_PROXY_URLS))
        if settings.BYBIT_PROXY_URL:
            proxies.append(settings.BYBIT_PROXY_URL)
    elif exchange == "okx":
        proxies.extend(_parse_proxy_list(settings.OKX_PROXY_URLS))
        if settings.OKX_PROXY_URL:
            proxies.append(settings.OKX_PROXY_URL)

    proxies.extend(_parse_proxy_list(settings.PROXY_URLS))
    if settings.PROXY_URL:
        proxies.append(settings.PROXY_URL)

    deduped: List[str] = []
    for proxy in proxies:
        if proxy and proxy not in deduped:
            deduped.append(proxy)
    return deduped


def available_proxies(exchange_name: str) -> List[str]:
    """Return proxies that are not currently in cooldown for the exchange."""
    proxies = _configured_proxies(exchange_name)
    if not proxies:
        return []

    now = time.time()
    failures = _PROXY_FAILURES[exchange_name.lower()]
    available = [proxy for proxy in proxies if failures.get(proxy, 0) <= now]
    if not available and proxies:
        logger.warning(
            "All %d proxies for %s are in cooldown — will try them anyway",
            len(proxies),
            exchange_name,
        )
    return available or proxies


def get_proxy_candidates(
    exchange_name: str,
    *,
    max_candidates: int = 3,
    include_direct_fallback: bool = False,
    direct_first: bool = False,
) -> List[Optional[str]]:
    """Return a rotated list of proxy candidates for an exchange.

    When *direct_first* is True, a direct connection (no proxy) is tried
    before any proxy.  This is useful for Bybit/OKX which do not geo-block
    cloud IPs, so a direct request often succeeds while all configured
    proxies are dead.
    """
    exchange = exchange_name.lower()
    proxies = available_proxies(exchange)

    candidates: List[Optional[str]] = []

    if direct_first:
        candidates.append(None)

    if proxies:
        start_idx = _PROXY_INDEX[exchange] % len(proxies)
        ordered = proxies[start_idx:] + proxies[:start_idx]
        _PROXY_INDEX[exchange] += 1
        candidates.extend(ordered[:max_candidates])
    elif not include_direct_fallback and not direct_first:
        # No proxies at all and direct not requested — still try direct
        candidates.append(None)

    if include_direct_fallback and None not in candidates:
        candidates.append(None)

    return candidates or [None]


def mark_proxy_failure(exchange_name: str, proxy_url: Optional[str]) -> None:
    """Put a failing proxy into cooldown."""
    if not proxy_url:
        return

    exchange = exchange_name.lower()
    _PROXY_FAILURES[exchange][proxy_url] = time.time() + settings.PROXY_COOLDOWN_SECONDS
    logger.warning(
        "Proxy cooldown enabled for %s on %s",
        mask_proxy(proxy_url),
        exchange,
    )


def mark_proxy_success(exchange_name: str, proxy_url: Optional[str]) -> None:
    """Clear proxy cooldown after a successful request."""
    if not proxy_url:
        return

    exchange = exchange_name.lower()
    failures = _PROXY_FAILURES[exchange]
    if proxy_url in failures:
        failures.pop(proxy_url, None)
        logger.info("Proxy recovered for %s on %s", mask_proxy(proxy_url), exchange)

