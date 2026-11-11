
import os
import requests
from datetime import datetime, timezone

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


def get_common_stocks(symbols):
    # Alpaca 종목 정보에서 ETF 제외
    assets = api_get(
        TRADING_URL,
        "/v2/assets",
        {"status": "active", "asset_class": "us_equity"}
    )

    allowed = set(symbols)
    common = set()

    for asset in assets:
        symbol = asset.get("symbol")
        if symbol not in allowed:
            continue

        if not asset.get("tradable", False):
            continue

        # Alpaca가 제공하는 종목명 기반 보조 필터
        name = (asset.get("name") or "").upper()

        blocked = [
            "ETF",
            "ETN",
            "EXCHANGE TRADED FUND",
            "EXCHANGE-TRADED FUND",
            "TRUST",
            "LEVERAGED",
            "INVERSE",
            "2X ",
            "3X "
        ]

        if any(word in name for word in blocked):
            continue

        common.add(symbol)

    print("Filtered candidates:", len(common))
    return sorted(common)


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

    if age < 0 or age > 1800:
        return None

    change = (price / prev_close - 1) * 100
    volume_ratio = volume / prev_volume

    if 1 <= change <= 10 and volume_ratio >= 0.3:
        category = "🔥 초기 상승 관심"
    elif 10 < change <= 25 and volume_ratio >= 0.5:
        category = "🚀 상승 돌파 관찰"
    elif change > 25 and volume_ratio >= 0.7:
        category = "⚠️ 급등 종목 / 추격 주의"
    else:
        return None

    return {
        "change": change,
        "message": (
            f"{category}\n"
            f"종목: {symbol}\n"
            f"가격: ${price:.4f}\n"
            f"전일 대비: {change:+.2f}%\n"
            f"당일/전일 거래량: {volume_ratio:.2f}배\n"
            f"최근 거래(UTC): {timestamp}\n"
            f"데이터: Alpaca IEX\n"
            f"※ 매수 신호 아님"
        )
    }


def main():
    print("US Stock Radar started")

    if not KEY or not SECRET:
        raise RuntimeError("Alpaca API keys missing")

    symbols = discover_symbols()

    if not symbols:
        print("No candidates discovered")
        return

    symbols = get_common_stocks(symbols)

    if not symbols:
        print("No common-stock candidates")
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
