#!/usr/bin/env python3
"""
Proxy checker for Crypto Pump Detector.

Tests each configured proxy against exchange APIs and reports
which ones are alive, which are geo-blocked, and which are dead.

Usage:
    python3 scripts/check_proxies.py                  # check all proxies from .env
    python3 scripts/check_proxies.py --fetch           # also fetch fresh free proxies
    python3 scripts/check_proxies.py --proxy http://1.2.3.4:8080  # test single proxy

Requires: aiohttp (already in requirements.txt)
"""

import asyncio
import os
import sys
import time
import argparse
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urlparse

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp


@dataclass
class ProxyResult:
    proxy: str
    exchange: str
    status: str          # "OK" | "GEO_BLOCKED" | "AUTH_REQUIRED" | "TIMEOUT" | "DEAD" | "CONNECTION_REFUSED"
    response_time_ms: int = 0
    error: str = ""


# ── Endpoints to test (public, no auth) ──────────────────────────────────

TEST_ENDPOINTS = {
    "binance": {
        "url": "https://fapi.binance.com/fapi/v1/ticker/24hr",
        "params": {"symbol": "BTCUSDT"},
        "weight": 1,
    },
    "bybit": {
        "url": "https://api.bybit.com/v5/market/tickers",
        "params": {"category": "linear", "symbol": "BTCUSDT"},
        "weight": 1,
    },
    "okx": {
        "url": "https://www.okx.com/api/v5/market/ticker",
        "params": {"instId": "BTC-USDT-SWAP"},
        "weight": 1,
    },
}


async def test_proxy(
    session: aiohttp.ClientSession,
    proxy: str,
    exchange: str,
    timeout_seconds: int = 10,
) -> ProxyResult:
    """Test a single proxy against one exchange endpoint."""
    ep = TEST_ENDPOINTS[exchange]
    start = time.monotonic()

    kwargs = {"timeout": aiohttp.ClientTimeout(total=timeout_seconds)}
    if proxy and proxy.lower() != "direct":
        kwargs["proxy"] = proxy

    try:
        async with session.get(ep["url"], params=ep["params"], **kwargs) as resp:
            elapsed = int((time.monotonic() - start) * 1000)

            if resp.status == 200:
                return ProxyResult(proxy=proxy, exchange=exchange, status="OK", response_time_ms=elapsed)
            elif resp.status == 451:
                return ProxyResult(proxy=proxy, exchange=exchange, status="GEO_BLOCKED", response_time_ms=elapsed,
                                   error="451 Unavailable For Legal Reasons")
            elif resp.status == 407:
                return ProxyResult(proxy=proxy, exchange=exchange, status="AUTH_REQUIRED", response_time_ms=elapsed,
                                   error="407 Proxy Authentication Required")
            elif resp.status == 403:
                return ProxyResult(proxy=proxy, exchange=exchange, status="GEO_BLOCKED", response_time_ms=elapsed,
                                   error="403 Forbidden (IP blocked)")
            elif resp.status == 429:
                return ProxyResult(proxy=proxy, exchange=exchange, status="OK", response_time_ms=elapsed,
                                   error="429 Rate limited (proxy works but throttled)")
            else:
                return ProxyResult(proxy=proxy, exchange=exchange, status="DEAD", response_time_ms=elapsed,
                                   error=f"HTTP {resp.status}")
    except aiohttp.ClientProxyConnectionError:
        return ProxyResult(proxy=proxy, exchange=exchange, status="DEAD", error="Connection refused by proxy")
    except aiohttp.ClientConnectorError:
        return ProxyResult(proxy=proxy, exchange=exchange, status="DEAD", error="Cannot connect to proxy")
    except asyncio.TimeoutError:
        return ProxyResult(proxy=proxy, exchange=exchange, status="TIMEOUT", error="Timed out")
    except Exception as e:
        return ProxyResult(proxy=proxy, exchange=exchange, status="DEAD", error=str(e)[:120])


def parse_proxies_from_env() -> dict:
    """Parse proxy lists from environment variables."""
    from dotenv import load_dotenv
    load_dotenv()

    result = {}
    mapping = {
        "binance": ["BINANCE_PROXY_URLS", "BINANCE_PROXY_URL"],
        "bybit": ["BYBIT_PROXY_URLS", "BYBIT_PROXY_URL"],
        "okx": ["OKX_PROXY_URLS", "OKX_PROXY_URL"],
    }
    # Fallback: PROXY_URLS / PROXY_URL
    fallback_vars = ["PROXY_URLS", "PROXY_URL"]

    for exchange, env_vars in mapping.items():
        proxies = []
        for var in env_vars:
            raw = os.environ.get(var, "")
            if raw:
                proxies.extend(_parse_proxy_list(raw))
        # Check fallback
        if not proxies:
            for var in fallback_vars:
                raw = os.environ.get(var, "")
                if raw:
                    proxies.extend(_parse_proxy_list(raw))
                    break
        result[exchange] = proxies

    return result


def _parse_proxy_list(raw: str) -> List[str]:
    """Parse comma/newline separated proxy list."""
    proxies = []
    for chunk in raw.replace(";", "\n").replace(",", "\n").splitlines():
        proxy = chunk.strip()
        if proxy and proxy not in proxies:
            proxies.append(proxy)
    return proxies


async def fetch_free_proxies() -> List[str]:
    """Fetch fresh free HTTP proxies from public API."""
    url = (
        "https://api.proxyscrape.com/v4/free-proxy-list/get"
        "?request=display_proxies"
        "&proxy_format=protocolipport"
        "&format=text"
        "&protocol=http"
        "&timeout=5000"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    proxies = []
                    for line in text.strip().splitlines():
                        line = line.strip()
                        if line and line.startswith("http"):
                            proxies.append(line)
                    return proxies[:50]  # limit to 50
    except Exception as e:
        print(f"⚠️  Failed to fetch free proxies: {e}")
    return []


def print_results(results: List[ProxyResult], show_ok: bool = True):
    """Pretty-print proxy test results."""
    ok = [r for r in results if r.status == "OK"]
    geo = [r for r in results if r.status == "GEO_BLOCKED"]
    auth = [r for r in results if r.status == "AUTH_REQUIRED"]
    dead = [r for r in results if r.status in ("DEAD", "TIMEOUT")]

    if ok:
        print(f"\n✅ WORKING ({len(ok)}):")
        for r in sorted(ok, key=lambda x: x.response_time_ms):
            print(f"   {r.response_time_ms:>5}ms  {r.proxy}  [{r.exchange}]" +
                  (f"  ⚠️ {r.error}" if r.error else ""))

    if geo:
        print(f"\n🚫 GEO_BLOCKED ({len(geo)}):")
        for r in geo:
            print(f"   {r.proxy}  [{r.exchange}]")

    if auth:
        print(f"\n🔒 AUTH_REQUIRED ({len(auth)}):")
        for r in auth:
            print(f"   {r.proxy}  [{r.exchange}]")

    if dead:
        print(f"\n💀 DEAD/TIMEOUT ({len(dead)}):")
        for r in dead[:10]:  # show first 10
            print(f"   {r.proxy}  [{r.exchange}] → {r.error}")
        if len(dead) > 10:
            print(f"   ... and {len(dead) - 10} more")

    # Summary
    total = len(results)
    print(f"\n{'─' * 50}")
    print(f"Total: {total} | ✅ {len(ok)} | 🚫 {len(geo)} | 🔒 {len(auth)} | 💀 {len(dead)}")


def generate_env_section(proxies: dict) -> str:
    """Generate .env section with working proxies."""
    lines = ["# === ПРОКСИ (обновлено скриптом check_proxies.py) ==="]
    lines.append("# Скопируйте в .env файл")
    lines.append("")

    for exchange, proxy_list in proxies.items():
        if proxy_list:
            var_name = f"{exchange.upper()}_PROXY_URLS"
            lines.append(f"# {exchange.capitalize()} ({len(proxy_list)} working proxies)")
            lines.append(f"{var_name}={','.join(proxy_list)}")
            lines.append("")

    return "\n".join(lines)


async def main():
    parser = argparse.ArgumentParser(description="Check proxy availability for exchange APIs")
    parser.add_argument("--fetch", action="store_true", help="Also fetch fresh free proxies and test them")
    parser.add_argument("--proxy", type=str, help="Test a single proxy (e.g. http://user:pass@host:port)")
    parser.add_argument("--exchange", choices=["binance", "bybit", "okx"], default=None,
                        help="Test against specific exchange only (default: all)")
    parser.add_argument("--save", action="store_true", help="Save working proxies to .env.proxies")
    parser.add_argument("--timeout", type=int, default=10, help="Timeout per proxy in seconds (default: 10)")
    args = parser.parse_args()

    exchanges = [args.exchange] if args.exchange else ["binance", "bybit", "okx"]

    if args.proxy:
        # Single proxy test
        print(f"🔍 Testing proxy: {args.proxy}")
        async with aiohttp.ClientSession() as session:
            for ex in exchanges:
                result = await test_proxy(session, args.proxy, ex, args.timeout)
                icon = {"OK": "✅", "GEO_BLOCKED": "🚫", "AUTH_REQUIRED": "🔒", "TIMEOUT": "⏱️", "DEAD": "💀"}
                print(f"   {icon.get(result.status, '?')} {result.status} [{ex}]"
                      f" ({result.response_time_ms}ms)" +
                      (f" — {result.error}" if result.error else ""))
        return

    # Parse configured proxies
    configured = parse_proxies_from_env()
    all_proxies = []
    seen = set()
    for exchange in exchanges:
        for proxy in configured.get(exchange, []):
            if proxy not in seen:
                all_proxies.append((proxy, exchange))
                seen.add(proxy)

    if not all_proxies and not args.fetch:
        print("⚠️  No proxies found in environment variables.")
        print("   Set BINANCE_PROXY_URLS, BYBIT_PROXY_URLS, or PROXY_URLS in .env")
        print("   Or run with --fetch to test free public proxies.")
        return

    print(f"🔍 Testing {len(all_proxies)} configured proxies...")

    # Test configured proxies
    results: List[ProxyResult] = []
    async with aiohttp.ClientSession() as session:
        # Test in parallel batches of 5
        for i in range(0, len(all_proxies), 5):
            batch = all_proxies[i:i+5]
            tasks = [test_proxy(session, proxy, ex, args.timeout) for proxy, ex in batch]
            batch_results = await asyncio.gather(*tasks)
            results.extend(batch_results)
            # Progress
            tested = min(i + 5, len(all_proxies))
            print(f"   Tested {tested}/{len(all_proxies)}...", end="\r")

    print_results(results)

    # Fetch fresh free proxies if requested
    if args.fetch:
        print(f"\n{'─' * 50}")
        print("🌐 Fetching fresh free proxies...")
        free_proxies = await fetch_free_proxies()
        print(f"   Got {len(free_proxies)} free proxies, testing against {', '.join(exchanges)}...")

        free_results: List[ProxyResult] = []
        async with aiohttp.ClientSession() as session:
            for i in range(0, len(free_proxies), 5):
                batch = free_proxies[i:i+5]
                tasks = []
                for proxy in batch:
                    for ex in exchanges:
                        tasks.append(test_proxy(session, proxy, ex, args.timeout))
                batch_results = await asyncio.gather(*tasks)
                free_results.extend(batch_results)
                tested = min(i + 5, len(free_proxies))
                print(f"   Tested {tested}/{len(free_proxies)}...", end="\r")

        print_results(free_results, show_ok=True)

    # Save working proxies
    if args.save:
        working = {}
        for r in results:
            if r.status == "OK" and r.response_time_ms < 5000:
                working.setdefault(r.exchange, []).append(r.proxy)

        if working:
            output = generate_env_section(working)
            with open(".env.proxies", "w") as f:
                f.write(output + "\n")
            print(f"\n💾 Working proxies saved to .env.proxies")
            print(f"   Copy the relevant lines into your .env file")
        else:
            print(f"\n⚠️  No working proxies to save")


if __name__ == "__main__":
    asyncio.run(main())
