
import os
import json
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

KEY = os.getenv("ALPACA_KEY", "")
SECRET = os.getenv("ALPACA_SECRET", "")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL", "")

DATA_URL = "https://data.alpaca.markets"
TRADING_URL = "https://paper-api.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET
}

MIN_PRICE = 0.01
MAX_PRICE = 7.00

STATE_FILE = Path("alert_state.json")
COOLDOWN_SECONDS = 3600


def api_get(base, path, params=None):
    response = requests.get(
        base + path,
        headers=HEADERS,
        params=params,
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def send_discord(message):
    if not DISCORD:
        print("Discord webhook missing")
        return False

    response = requests.post(
        DISCORD,
        json={"content": message[:1900]},
        timeout=15
    )
    response.raise_for_status()
    print("Discord alert sent")
    return True


def discover_symbols():
    symbols = set()

    sources = [
        (
            "/v1beta1/screener/stocks/most-actives",
            {"top": 100, "by": "volume"},
            "most_actives"
        ),
        (
            "/v1beta1/screener/stocks/movers",
            {"top": 50},
            "gainers"
        )
    ]

    for path, params, field in sources:
        try:
            data = api_get(DATA_URL, path, params)

            for item in data.get(field, []):
                symbol = item.get("symbol")
                if symbol:
                    symbols.add(symbol)

        except requests.RequestException as error:
            print("Discovery failed:", error)

    print("Discovered candidates:", len(symbols))
    return sorted(symbols)


def filter_stocks(symbols):
    assets = api_get(
        TRADING_URL,
        "/v2/assets",
        {"status": "active", "asset_class": "us_equity"}
    )

    wanted = set(symbols)
    allowed = []

    blocked_words = [
        "ETF", "ETN", "EXCHANGE TRADED FUND",
        "EXCHANGE-TRADED FUND", "TRUST",
        "LEVERAGED", "INVERSE", "2X ", "3X "
    ]

    for asset in assets:
        symbol = asset.get("symbol")

        if symbol not in wanted:
            continue

        if not asset.get("tradable", False):
            continue

        name = (asset.get("name") or "").upper()

        if any(word in name for word in blocked_words):
            continue

        allowed.append(symbol)

    print("Filtered candidates:", len(allowed))
    return sorted(set(allowed))


def get_snapshots(symbols):
    snapshots = {}

    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]

        data = api_get(
            DATA_URL,
            "/v2/stocks/snapshots",
            {
                "symbols": ",".join(batch),
                "feed": "iex"
            }
        )

        snapshots.update(data)

    print("Snapshots received:", len(snapshots))
    return snapshots


def get_recent_bars(symbol):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=35)

    data = api_get(
        DATA_URL,
        f"/v2/stocks/{symbol}/bars",
        {
            "timeframe": "1Min",
            "start": start.isoformat(),
            "end": end.isoformat(),
            "limit": 100,
            "feed": "iex"
        }
    )

    return data.get("bars", [])


def analyze(symbol, snapshot):
    trade = snapshot.get("latestTrade") or {}
    previous = snapshot.get("prevDailyBar") or {}

    price = trade.get("p")
    prev_close = previous.get("c")

    if not price or not prev_close:
        return None

    if not MIN_PRICE <= price <= MAX_PRICE:
        return None

    change = (price / prev_close - 1) * 100

    # 이미 크게 오른 종목은 초기 탐색에서 제외
    if change < -3 or change > 25:
        return None

    try:
        bars = get_recent_bars(symbol)
    except requests.RequestException as error:
        print(symbol, "bar error:", error)
        return None

    if len(bars) < 4:
        return None

    now = datetime.now(timezone.utc)

    def parse_time(bar):
        return datetime.fromisoformat(
            bar["t"].replace("Z", "+00:00")
        )

    recent = []
    previous_five = []

    for bar in bars:
        try:
            bar_time = parse_time(bar)
        except (ValueError, KeyError, TypeError):
            continue

        age = (now - bar_time).total_seconds()

        if 0 <= age < 300:
            recent.append(bar)
        elif 300 <= age < 600:
            previous_five.append(bar)

    if len(recent) < 2 or len(previous_five) < 2:
        return None

    recent.sort(key=lambda b: b["t"])

    first_price = recent[0].get("o")
    last_price = recent[-1].get("c")

    if not first_price or not last_price:
        return None

    five_min_change = (
        last_price / first_price - 1
    ) * 100

    recent_volume = sum(
        bar.get("v", 0) for bar in recent
    )

    previous_volume = sum(
        bar.get("v", 0) for bar in previous_five
    )

    if previous_volume <= 0:
        return None

    volume_ratio = recent_volume / previous_volume

    # 최근 5분 가격과 거래량 동시 증가
    if five_min_change >= 2 and volume_ratio >= 2:
        category = "🔥 강한 초기 급등 신호"

    elif five_min_change >= 1 and volume_ratio >= 1.5:
        category = "🚀 초기 상승 관심"

    else:
        return None

    return {
        "symbol": symbol,
        "score": five_min_change * volume_ratio,
        "message": (
            f"{category}\n"
            f"종목: {symbol}\n"
            f"가격: ${price:.4f}\n"
            f"전일 대비: {change:+.2f}%\n"
            f"최근 5분 상승률: {five_min_change:+.2f}%\n"
            f"최근 5분 거래량 비율: {volume_ratio:.2f}배\n"
            f"데이터: Alpaca IEX\n"
            f"※ 매수 신호 아님"
        )
    }


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except (ValueError, OSError):
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state),
        encoding="utf-8"
    )


def main():
    print("US Stock Radar started")

    if not KEY or not SECRET:
        raise RuntimeError("Alpaca API keys missing")

    symbols = discover_symbols()

    if not symbols:
        print("No candidates discovered")
        return

    symbols = filter_stocks(symbols)

    if not symbols:
        print("No filtered candidates")
        return

    snapshots = get_snapshots(symbols)

    signals = []

    for symbol in symbols:
        snapshot = snapshots.get(symbol) or {}

        result = analyze(symbol, snapshot)

        if result:
            signals.append(result)

    print("Early signals found:", len(signals))

    if not signals:
        print("초기 급등 조건 충족 신호 없음")
        return

    signals.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    state = load_state()
    now_ts = time.time()
    selected = []

    for signal in signals:
        symbol = signal["symbol"]

        last_sent = state.get(symbol, 0)

        if now_ts - last_sent < COOLDOWN_SECONDS:
            continue

        selected.append(signal)

        if len(selected) >= 3:
            break

    if not selected:
        print("All signals in cooldown")
        return

    message = "📡 미국 저가주 초기 급등 탐색\n\n"

    for signal in selected:
        addition = signal["message"] + "\n\n"

        if len(message + addition) > 1900:
            break

        message += addition

    if send_discord(message):
        for signal in selected:
            state[signal["symbol"]] = now_ts

        save_state(state)

    print("Scan completed")


if __name__ == "__main__":
    main()
