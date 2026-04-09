# Как подключить новые метрики в scorer

## Что добавлено (без CoinGlass, всё бесплатно)

### coingecko_client.py
| Метод | Эндпоинт | Стоимость |
|---|---|---|
| `get_trending_symbols()` | `/search/trending` | 1 вызов на весь цикл |
| `get_top_gainers_symbols()` | `/coins/markets` | уже вызывается |
| `get_market_cap_and_rank_map()` | `/coins/markets` | тот же вызов |

### binance_client.py — новые поля в MarketData
| Поле | Эндпоинт | Что даёт |
|---|---|---|
| `top_trader_ls_ratio` | `/futures/data/topLongShortPositionRatio` | "умные деньги" |
| `taker_buy_ratio` | `/futures/data/takerBuySellRatio` | агрессивные покупки |
| `recent_liquidations_usd` | `/fapi/v1/allForceOrders` | ликвидации $ |
| `liq_side` | (вычисляется) | SHORT или LONG доминирует |
| `oi_trend` | (из существующего OI hist) | growing/shrinking/flat |

---

## Как добавить в scorer (пример логики)

```python
# В начале цикла сканирования — один раз для всех символов:
trending_symbols = await coingecko.get_trending_symbols()
# trending_symbols = {'SOL', 'PEPE', 'WIF', ...}

# Для каждого сигнала:
base_symbol = signal.symbol.replace('USDT', '')  # 'SOLUSDT' -> 'SOL'

# === ФАКТОР 1: CoinGecko trending (+1.0) ===
if base_symbol in trending_symbols:
    score += 1.0
    factors.append('🔥 CoinGecko trending top-7')

# === ФАКТОР 2: Taker buy/sell ratio (+1.0) ===
if data.taker_buy_ratio:
    if data.taker_buy_ratio > 1.5 and signal.is_pump:
        score += 1.0
        factors.append(f'Агрессивные покупки: ratio {data.taker_buy_ratio:.2f}')
    elif data.taker_buy_ratio < 0.67 and not signal.is_pump:
        score += 1.0
        factors.append(f'Агрессивные продажи: ratio {data.taker_buy_ratio:.2f}')

# === ФАКТОР 3: Top trader vs retail divergence (+1.0) ===
if data.top_trader_ls_ratio and data.long_short_ratio:
    top = data.top_trader_ls_ratio
    retail = data.long_short_ratio
    if top > 1.5 and retail < 1.0 and signal.is_pump:
        score += 1.0
        factors.append(f'Топ-трейдеры лонг ({top:.2f}), ритейл шорт ({retail:.2f})')
    elif top < 0.7 and retail > 1.2 and not signal.is_pump:
        score += 1.0
        factors.append(f'Топ-трейдеры шорт ({top:.2f}), ритейл лонг ({retail:.2f})')

# === ФАКТОР 4: Ликвидации (+1.5) ===
if data.recent_liquidations_usd and data.recent_liquidations_usd > 500_000:
    usd_m = data.recent_liquidations_usd / 1_000_000
    if data.liq_side == 'SHORT' and signal.is_pump:
        score += 1.5
        factors.append(f'💥 Шорт-сквиз: ${usd_m:.1f}M ликвидировано')
    elif data.liq_side == 'LONG' and not signal.is_pump:
        score += 1.5
        factors.append(f'💥 Лонг-ликвидации: ${usd_m:.1f}M')

# === ФАКТОР 5: OI тренд (+0.5 / -0.5) ===
if data.oi_trend == 'growing' and signal.is_pump:
    score += 0.5
    factors.append('OI стабильно растёт (4 периода)')
elif data.oi_trend == 'shrinking' and signal.is_pump:
    score -= 0.5
    factors.append('⚠️ OI падает (возможно закрытие шортов, не новые лонги)')
```

---

## Итоговая шкала score после апгрейда

| Score | Смысл |
|---|---|
| 3.0–3.5 | Базовый сигнал (OI + volume + funding) |
| 4.0–4.5 | + подтверждение (trending / taker ratio) |
| 5.0+ | + ликвидации / дивергенция топ vs ритейл |

Порог отображения 3+ остаётся. Но теперь 5/5 = реально сильный сигнал.
