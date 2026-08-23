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
_DYNAMIC_PROXIES: Dict[str, List[str]] = defaultdict(list)  # exchange -> dynamic proxies


def create_session() -> aiohttp.ClientSession:
    """Create an aiohttp session shared by exchange clients."""
    connector = aiohttp.TCPConnector(
        limit=20,
        ttl_dns_cache=300,
        ssl=False,
    )
    return aiohttp.ClientSession(connector=connector)


# ── Simple TTL cache for slow-changing data ─────────────────────────────

class TTLCache:
    """In-memory dict with per-key TTL.  Thread-safe enough for asyncio
    (single-threaded event loop).
    """

    def __init__(self, default_ttl: int = 900):
        """default_ttl in seconds (15 min)."""
        self._store: Dict[str, tuple] = {}  # key -> (value, expiry_ts)
        self._default_ttl = default_ttl

    def get(self, key: str) -> Optional[Any]:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expiry = entry
        if time.time() > expiry:
            del self._store[key]
            return None
        return value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._store[key] = (value, time.time() + (ttl or self._default_ttl))

    def clear(self) -> None:
        self._store.clear()


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

    # Add dynamic proxies (from health checker)
    proxies.extend(_DYNAMIC_PROXIES.get(exchange, []))

    deduped: List[str] = []
    for proxy in proxies:
        if proxy and proxy not in deduped:
            deduped.append(proxy)
    return deduped


def add_dynamic_proxy(exchange_name: str, proxy_url: str) -> bool:
    """Add a proxy dynamically (from health checker). Returns True if added."""
    exchange = exchange_name.lower()
    if proxy_url not in _DYNAMIC_PROXIES[exchange]:
        _DYNAMIC_PROXIES[exchange].append(proxy_url)
        logger.info("Dynamic proxy added for %s: %s", exchange, mask_proxy(proxy_url))
        return True
    return False


def remove_proxy(exchange_name: str, proxy_url: str) -> bool:
    """Remove a proxy from the dynamic pool."""
    exchange = exchange_name.lower()
    before = len(_DYNAMIC_PROXIES[exchange])
    _DYNAMIC_PROXIES[exchange] = [p for p in _DYNAMIC_PROXIES[exchange] if p != proxy_url]
    removed = len(_DYNAMIC_PROXIES[exchange]) < before
    if removed:
        logger.info("Dynamic proxy removed for %s: %s", exchange, mask_proxy(proxy_url))
    return removed


def get_all_proxies(exchange_name: str) -> List[str]:
    """Get all configured + dynamic proxies for an exchange."""
    return _configured_proxies(exchange_name)


def get_proxy_pool_stats() -> Dict[str, Dict[str, int]]:
    """Get proxy pool statistics."""
    stats = {}
    for exchange in set(list(_DYNAMIC_PROXIES.keys()) + ["binance", "bybit", "okx"]):
        all_proxies = _configured_proxies(exchange)
        now = time.time()
        failures = _PROXY_FAILURES[exchange]
        active = [p for p in all_proxies if failures.get(p, 0) <= now]
        cooldown = [p for p in all_proxies if failures.get(p, 0) > now]
        stats[exchange] = {
            "total": len(all_proxies),
            "active": len(active),
            "cooldown": len(cooldown),
            "dynamic": len(_DYNAMIC_PROXIES.get(exchange, [])),
        }
    return stats


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

