"""Proxy-aware aiohttp session factory for bypassing IP bans on Railway."""

import aiohttp
import logging
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)


def create_session(proxy_url: Optional[str] = None) -> aiohttp.ClientSession:
    """Create aiohttp session. Proxy is applied per-request, not on session."""
    connector = aiohttp.TCPConnector(
        limit=20,
        ttl_dns_cache=300,
        ssl=False  # Some proxies have SSL issues; we verify at proxy level
    )
    return aiohttp.ClientSession(connector=connector)


def get_proxy_url() -> Optional[str]:
    """Get proxy URL from settings."""
    proxy = settings.PROXY_URL
    if proxy:
        logger.info(f"Using proxy: {proxy.split('@')[-1] if '@' in proxy else proxy}")
    return proxy


async def proxy_request(
    session: aiohttp.ClientSession,
    url: str,
    params: dict = None,
    proxy_url: Optional[str] = None
) -> aiohttp.ClientResponse:
    """Make a GET request through proxy if configured."""
    proxy = proxy_url or get_proxy_url()
    kwargs = {"params": params or {}}
    if proxy:
        kwargs["proxy"] = proxy
    return await session.get(url, **kwargs)
