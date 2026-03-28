# 🚀 Crypto Pump Detector — Railway Edition

Бот для детекции памп/дамп сигналов на криптобиржах. Мониторит Open Interest, объёмы, фандинг и L/S ratio, шлёт алерты в Telegram.

## ⚡ Быстрый деплой на Railway

### 1. Подготовка Telegram бота

1. Открой [@BotFather](https://t.me/BotFather) → `/newbot` → сохрани токен
2. Создай группу, добавь бота **администратором**
3. Включи топики, создай топик для сигналов
4. Получи `Chat ID` и `Thread ID`:
   - Напиши в группу, открой `https://api.telegram.org/bot<ТОКЕН>/getUpdates`
   - `chat.id` = Chat ID (начинается с `-100`)
   - `message_thread_id` = Thread ID

### 2. Получи прокси (обязательно для Binance!)

Binance блокирует IP Railway. Нужен HTTP/SOCKS5 прокси.

**Бесплатные варианты:**
- [Webshare.io](https://www.webshare.io/) — 10 бесплатных прокси (рекомендую!)
- [ProxyScrape](https://proxyscrape.com/) — бесплатные прокси (менее надёжные)

**Дешёвые ($1-3/мес):**
- [Proxy6.net](https://proxy6.net/) — IPv6 прокси от $0.5
- [Smartproxy](https://smartproxy.com/) — residential прокси (trial)

### 3. Деплой на Railway

1. Залей проект на GitHub
2. Зайди на [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub**
3. Выбери репо
4. Добавь переменные окружения (**Variables** tab):

```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHI...
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_THREAD_ID=123
PROXY_URL=http://user:pass@proxy:port
SCAN_INTERVAL=300
MIN_SIGNAL_SCORE=3
TOP_N_SYMBOLS=100
```

5. Railway автоматически задеплоит через Dockerfile

### 4. Проверка

В Railway логах должно появиться:
```
Crypto Pump Detector Starting
Fetching market cap data from CoinGecko...
Binance: XX symbols
Bybit: XX symbols
Bot started and sent startup message
```

## 🔧 Настройки

| Переменная | По умолчанию | Описание |
|---|---|---|
| `PROXY_URL` | — | HTTP/SOCKS5 прокси для обхода блокировок |
| `SCAN_INTERVAL` | 300 | Интервал сканирования (секунды) |
| `OI_CHANGE_THRESHOLD` | 5.0 | Порог изменения OI (%) |
| `PRICE_CHANGE_THRESHOLD` | 1.0 | Порог изменения цены (%) |
| `VOLUME_CHANGE_THRESHOLD` | 50.0 | Порог спайка объёма (%) |
| `MIN_SIGNAL_SCORE` | 3 | Мин. оценка для сигнала (0-5) |
| `TOP_N_SYMBOLS` | 100 | Кол-во пар для мониторинга |
| `EXCHANGES` | binance,bybit | Биржи |

## 🧪 Тестирование прокси

```bash
# Локально
PROXY_URL=http://user:pass@host:port python test_proxy.py

# На Railway — проверь логи при старте
```

## 📁 Структура

```
├── config/settings.py          # Конфигурация
├── src/
│   ├── exchanges/
│   │   ├── proxy_session.py    # Прокси-модуль
│   │   ├── binance_client.py   # Binance API + прокси
│   │   ├── bybit_client.py     # Bybit API + прокси
│   │   └── coingecko_client.py # CoinGecko API
│   ├── detector/
│   │   └── signal_detector.py  # Алгоритм детекции
│   └── bot/
│       └── telegram_bot.py     # Telegram бот
├── main.py                     # Точка входа
├── test_proxy.py               # Тест прокси
├── Dockerfile                  # Docker для Railway
├── railway.json                # Конфиг Railway
└── requirements.txt            # Зависимости
```
