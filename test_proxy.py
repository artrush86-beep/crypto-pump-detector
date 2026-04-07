"""Test script to verify proxy and API connectivity."""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Optional

import aiohttp

sys.path.insert(0, os.path.dirname(__file__))


def _parse_first_proxy(*values: Optional[str]) -> Optional[str]:
    """Return first proxy from single-value or list-based env vars."""
    for value in values:
        if not value:
            continue
        normalized = value.replace(";", "\n").replace(",", "\n")
        for chunk in normalized.splitlines():
            proxy = chunk.strip()
            if proxy:
                return proxy
    return None


async def test_api(name: str, url: str, proxy: Optional[str] = None) -> bool:
    """Test single API endpoint."""
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            kwargs = {}
            if proxy:
                kwargs["proxy"] = proxy

            async with session.get(url, **kwargs) as resp:
                data = await resp.text()
                status = "✅" if resp.status == 200 else "⚠️"
                route = proxy.split("@")[-1] if proxy and "@" in proxy else (proxy or "direct")
                print(f"{status} {name} via {route}: HTTP {resp.status} ({len(data)} bytes)")
                if resp.status != 200:
                    print(f"   Response: {data[:200]}")
                return resp.status == 200
    except Exception as exc:
        route = proxy.split("@")[-1] if proxy and "@" in proxy else (proxy or "direct")
        print(f"❌ {name} via {route}: {exc}")
        return False


async def main():
    global_proxy = _parse_first_proxy(os.environ.get("PROXY_URLS"), os.environ.get("PROXY_URL"))
    binance_proxy = _parse_first_proxy(
        os.environ.get("BINANCE_PROXY_URLS"),
        os.environ.get("BINANCE_PROXY_URL"),
        global_proxy,
    )
    bybit_proxy = _parse_first_proxy(
        os.environ.get("BYBIT_PROXY_URLS"),
        os.environ.get("BYBIT_PROXY_URL"),
        global_proxy,
    )

    print("=" * 60)
    print("Crypto Pump Detector - Connectivity Test")
    print("=" * 60)

    print(f"Binance proxy: {(binance_proxy or 'direct').split('@')[-1] if binance_proxy else 'direct'}")
    print(f"Bybit proxy: {(bybit_proxy or 'direct').split('@')[-1] if bybit_proxy else 'direct'}")

    apis = {
        "Binance Futures": (
            "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
            binance_proxy,
        ),
        "Bybit V5": (
            "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
            bybit_proxy,
        ),
        "CoinGecko": (
            "https://api.coingecko.com/api/v3/ping",
            None,
        ),
        "Telegram": (
            "https://api.telegram.org/",
            None,
        ),
    }

    for name, (url, proxy) in apis.items():
        await test_api(name, url, proxy)

    print("\n" + "=" * 60)
    print("Если Binance доступен только через proxy, а Bybit работает direct — это нормальный прод-режим для Railway.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

