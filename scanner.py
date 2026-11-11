

import os
import sys
from datetime import datetime, timezone
import requests

BASE = "https://data.alpaca.markets"
KEY = os.getenv("ALPACA_KEY", "")
SECRET = os.getenv("ALPACA_SECRET", "")
BOT = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# 첫 테스트용 감시 목록. 전체시장 검색은 아님.
SYMBOLS = [
    "AMIX", "PEPG", "WKSP", "SKYE",
    "CABA", "CLOV", "AMC", "AIOT"
]

def get_snapshots():
    headers = {
        "APCA-API-KEY-ID": KEY,
        "APCA-API-SECRET-KEY": SECRET
    }
    response = requests.get(
        BASE + "/v2/stocks/snapshots",
        params={
            "symbols": ",".join(SYMBOLS),
            "feed": "iex"
        },
        headers=headers,
        timeout=25
    )
    response.raise_for_status()
    return response.json()

def send_message(message):
    if not BOT or not CHAT:
        print("Telegram not configured")
        return

    response = requests.post(
        f"https://api.telegram.org/bot{BOT}/sendMessage",
        json={"chat_id": CHAT, "text": message},
        timeout=15
    )
    response.raise_for_status()

def analyze(symbol, data):
    trade = data.get("latestTrade") or {}
    today = data.get("dailyBar") or {}
    prev = data.get("prevDailyBar") or {}

    price = trade.get("p")
    previous_close = prev.get("c")
    volume = today.get("v")
    previous_volume = prev.get("v")
    timestamp = trade.get("t")

    if not all([
        price, previous_close,
        volume, previous_volume, timestamp
    ]):
        return None

    # 실제 마지막 거래 시각 검증
    try:
        traded_at = datetime.fromisoformat(
            timestamp.replace("Z", "+00:00")
        )
        age = (
            datetime.now(timezone.utc) - traded_at
        ).total_seconds()
    except (ValueError, TypeError):
        return None

    if age < 0 or age > 1800:
        return None

    change = (
        (price / previous_close) - 1
    ) * 100

    # 누적 거래량 / 전일 전체 거래량
    # 동일 시간대 RVOL이 아님
    volume_ratio = volume / previous_volume

    if not 0.01 <= price <= 7:
        return None

    if volume_ratio < 2:
        return None

    if -3 <= change <= 7:
        category = "초기 관심"
    elif change >= 10:
        category = "급등 관찰 / 추격 주의"
    else:
        return None

    return (
        f"{category}\n"
        f"종목: {symbol}\n"
        f"가격: ${price:.4f}\n"
        f"등락률: {change:+.2f}%\n"
        f"누적/전일 거래량: {volume_ratio:.1f}배\n"
        f"거래 시각(UTC): {timestamp}\n"
        f"데이터: Alpaca IEX\n"
        f"※ 매수 신호 아님"
    )

def main():
    if not KEY or not SECRET:
        print("Alpaca API credentials missing")
        sys.exit(1)

    snapshots = get_snapshots()
    signals = []

    for symbol in SYMBOLS:
        result = analyze(
            symbol, snapshots.get(symbol) or {}
        )
        if result:
            signals.append(result)

    if not signals:
        print("검증된 조건 충족 신호 없음")
        return

    message = "미국 주식 감시 결과\n\n"
    message += "\n\n".join(signals[:5])

    print(message)
    send_message(message)

if __name__ == "__main__":
    main()
