"""Test script to verify proxy and API connectivity."""

import asyncio
import aiohttp
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


async def test_api(name: str, url: str, proxy: str = None):
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
                print(f"{status} {name}: HTTP {resp.status} ({len(data)} bytes)")
                if resp.status != 200:
                    print(f"   Response: {data[:200]}")
                return resp.status == 200
    except Exception as e:
        print(f"❌ {name}: {e}")
        return False


async def main():
    proxy = os.environ.get("PROXY_URL")
    
    print("=" * 50)
    print("Crypto Pump Detector - Connectivity Test")
    print("=" * 50)
    
    if proxy:
        masked = proxy.split("@")[-1] if "@" in proxy else proxy
        print(f"\n🔄 Proxy: {masked}")
    else:
        print("\n⚠️  No PROXY_URL set — testing direct connection")
    
    apis = {
        "Binance Futures": "https://fapi.binance.com/fapi/v1/ticker/price?symbol=BTCUSDT",
        "Bybit V5": "https://api.bybit.com/v5/market/tickers?category=linear&symbol=BTCUSDT",
        "CoinGecko": "https://api.coingecko.com/api/v3/ping",
        "Telegram": "https://api.telegram.org/",
    }
    
    print("\n--- Без прокси (прямое соединение) ---")
    for name, url in apis.items():
        await test_api(name, url)
    
    if proxy:
        print(f"\n--- Через прокси ---")
        for name, url in apis.items():
            await test_api(f"{name} (proxy)", url, proxy)
    
    print("\n" + "=" * 50)
    print("Если Binance ❌ без прокси, но ✅ через прокси — всё настроено верно!")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
