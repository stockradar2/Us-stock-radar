
import os
from datetime import datetime, timezone
import requests

KEY = os.getenv("ALPACA_KEY", "")
SECRET = os.getenv("ALPACA_SECRET", "")
DISCORD = os.getenv("DISCORD_WEBHOOK_URL", "")

BASE = "https://data.alpaca.markets"

HEADERS = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET
}

# 미국 저가주 감시 가격: 달러 기준
MIN_PRICE = 0.01
MAX_PRICE = 7.00


def api_get(path, params=None):
    response = requests.get(
        BASE + path,
        headers=HEADERS,
        params=params,
        timeout=30
    )
    response.raise_for_status()
    return response.json()


def send_discord(message):
    if not DISCORD:
        print("Discord webhook missing")
        return

    response = requests.post(
        DISCORD,
        json={"content": message[:1900]},
        timeout=15
    )
    response.raise_for_status()
    print("Discord alert sent")


def discover_symbols():
    symbols = set()

    # 거래량 상위 종목 자동 검색
    try:
        data = api_get(
            "/v1beta1/screener/stocks/most-actives",
            {"top": 100, "by": "volume"}
        )

        for item in data.get("most_actives", []):
            symbol = item.get("symbol")
            if symbol:
                symbols.add(symbol)

    except requests.RequestException as error:
        print("Most-active search failed:", error)

    # 상승률 상위 종목 자동 검색
    try:
        data = api_get(
            "/v1beta1/screener/stocks/movers",
            {"top": 50}
        )

        for item in data.get("gainers", []):
            symbol = item.get("symbol")
            if symbol:
                symbols.add(symbol)

    except requests.RequestException as error:
        print("Mover search failed:", error)

    print("Discovered candidates:", len(symbols))
    return sorted(symbols)


def get_snapshots(symbols):
    snapshots = {}

    # 한 번에 최대 100종목 조회
    for start in range(0, len(symbols), 100):
        batch = symbols[start:start + 100]

        data = api_get(
            "/v2/stocks/snapshots",
            {
                "symbols": ",".join(batch),
                "feed": "iex"
            }
        )
        snapshots.update(data)

    return snapshots


def analyze(symbol, data):
    trade = data.get("latestTrade") or {}
    daily = data.get("dailyBar") or {}
    previous = data.get("prevDailyBar") or {}

    price = trade.get("p")
    prev_close = previous.get("c")
    volume = daily.get("v")
    prev_volume = previous.get("v")
    timestamp = trade.get("t")

    if not all([
        price, prev_close,
        volume, prev_volume, timestamp
    ]):
        return None

    if not MIN_PRICE <= price <= MAX_PRICE:
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

    # 30분 이내 실제 거래만 인정
    if age < 0 or age > 1800:
        return None

    change = (price / prev_close - 1) * 100

    # 전일 전체 거래량 대비 당일 누적 거래량
    volume_ratio = volume / prev_volume

    # 초기 상승 신호
    if 1 <= change <= 10 and volume_ratio >= 0.3:
        category = "🔥 초기 상승 관심"

    # 상승 돌파 관찰
    elif 10 < change <= 25 and volume_ratio >= 0.5:
        category = "🚀 상승 돌파 관찰"

    # 이미 크게 상승한 종목
    elif change > 25 and volume_ratio >= 0.7:
        category = "⚠️ 급등 종목 / 추격 주의"

    else:
        return None

    message = (
        f"{category}\n"
        f"종목: {symbol}\n"
        f"가격: ${price:.4f}\n"
        f"전일 종가 대비: {change:+.2f}%\n"
        f"당일/전일 거래량: {volume_ratio:.2f}배\n"
        f"최근 거래(UTC): {timestamp}\n"
        f"데이터: Alpaca IEX\n"
        f"※ 매수 신호 아님"
    )

    return {
        "symbol": symbol,
        "change": change,
        "message": message
    }


def main():
    print("US Stock Radar started")

    if not KEY or not SECRET:
        raise RuntimeError("Alpaca API keys missing")

    symbols = discover_symbols()

    if not symbols:
        print("No candidates discovered")
        return

    snapshots = get_snapshots(symbols)

    print("Snapshots received:", len(snapshots))

    signals = []

    for symbol in symbols:
        result = analyze(
            symbol,
            snapshots.get(symbol) or {}
        )
        if result:
            signals.append(result)

    if not signals:
        print("검증된 조건 충족 신호 없음")
        return

    # 상승률 낮은 초기 후보부터 표시
    signals.sort(key=lambda x: x["change"])

    message = "📡 미국 저가주 자동 탐색\n\n"

    for signal in signals[:3]:
        next_message = message + signal["message"] + "\n\n"
        if len(next_message) > 1900:
            break
        message = next_message

    print(message)
    send_discord(message)

    print("Scan completed")


if __name__ == "__main__":
    main()
