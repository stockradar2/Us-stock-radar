
import os
from datetime import datetime, timezone

import requests

# API 설정
ALPACA_KEY = os.getenv("ALPACA_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET", "")
DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")

BASE_URL = "https://data.alpaca.markets"

# 첫 테스트용 종목 8개
SYMBOLS = [
    "AMIX", "PEPG", "WKSP", "SKYE",
    "CABA", "CLOV", "AMC", "AIOT"
]


def send_discord(message):
    if not DISCORD_WEBHOOK:
        print("Discord webhook not configured")
        return

    response = requests.post(
        DISCORD_WEBHOOK,
        json={"content": message[:1900]},
        timeout=15
    )
    response.raise_for_status()
    print("Discord message sent successfully")


def get_snapshots():
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError("Alpaca API keys missing")

    headers = {
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET
    }

    response = requests.get(
        BASE_URL + "/v2/stocks/snapshots",
        params={
            "symbols": ",".join(SYMBOLS),
            "feed": "iex"
        },
        headers=headers,
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def analyze(symbol, data):
    trade = data.get("latestTrade") or {}
    daily = data.get("dailyBar") or {}
    previous = data.get("prevDailyBar") or {}

    price = trade.get("p")
    previous_close = previous.get("c")
    volume = daily.get("v")
    previous_volume = previous.get("v")
    timestamp = trade.get("t")

    if not all([
        price, previous_close,
        volume, previous_volume, timestamp
    ]):
        return None

    try:
        trade_time = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        age = (
            datetime.now(timezone.utc) - trade_time
        ).total_seconds()
    except (ValueError, TypeError):
        return None

    # 30분 넘게 지난 거래는 제외
    if age < 0 or age > 1800:
        return None

    change = (price / previous_close - 1) * 100

    # 전일 전체 거래량 대비 비율
    # 동일 시간대 상대 거래량(RVOL)은 아님
    volume_ratio = volume / previous_volume

    if not 0.01 <= price <= 7:
        return None

    if volume_ratio < 2:
        return None

    if -3 <= change <= 7:
        category = "🔥 초기 관심"
    elif change >= 10:
        category = "👀 급등 관찰 / 추격 주의"
    else:
        return None

    return (
        f"{category}\n"
        f"종목: {symbol}\n"
        f"현재가: ${price:.4f}\n"
        f"등락률: {change:+.2f}%\n"
        f"당일/전일 거래량: {volume_ratio:.1f}배\n"
        f"최근 거래 시각(UTC): {timestamp}\n"
        f"데이터: Alpaca IEX\n"
        f"※ 매수 신호 아님"
    )


def main():
    print("US Stock Radar started")

    snapshots = get_snapshots()
    print(f"Received data for {len(snapshots)} symbols")

    signals = []

    for symbol in SYMBOLS:
        result = analyze(
            symbol,
            snapshots.get(symbol) or {}
        )
        if result:
            signals.append(result)

    if signals:
        message = (
            "📡 US STOCK RADAR\n\n"
            + "\n\n".join(signals[:3])
        )
        print(message)
        send_discord(message)
    else:
        print("검증된 조건 충족 신호 없음")

    print("Scan completed")


if __name__ == "__main__":
    main()
