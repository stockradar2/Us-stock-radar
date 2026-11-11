
import os
import requests
from collections import Counter
from datetime import datetime, timezone, timedelta

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
        timeout=40
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
            print("Discovery error:", error)

    print("Discovered candidates:", len(symbols))
    return sorted(symbols)


def filter_stocks(symbols):
    assets = api_get(
        TRADING_URL,
        "/v2/assets",
        {
            "status": "active",
            "asset_class": "us_equity"
        }
    )

    wanted = set(symbols)
    allowed = []

    blocked = [
        "ETF", "ETN",
        "EXCHANGE TRADED FUND",
        "EXCHANGE-TRADED FUND",
        "TRUST", "LEVERAGED",
        "INVERSE", "2X ", "3X "
    ]

    for asset in assets:
        symbol = asset.get("symbol")

        if symbol not in wanted:
            continue

        if not asset.get("tradable", False):
            continue

        name = (asset.get("name") or "").upper()

        if any(word in name for word in blocked):
            continue

        allowed.append(symbol)

    result = sorted(set(allowed))
    print("Filtered candidates:", len(result))
    return result


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


def get_all_bars(symbols):
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=35)

    all_bars = {}

    # 여러 종목의 1분봉을 한꺼번에 요청
    # 페이지가 남아 있으면 이어서 조회
    for offset in range(0, len(symbols), 50):
        batch = symbols[offset:offset + 50]
        page_token = None

        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Min",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "feed": "iex"
            }

            if page_token:
                params["page_token"] = page_token

            data = api_get(
                DATA_URL,
                "/v2/stocks/bars",
                params
            )

            for symbol, bars in (
                data.get("bars") or {}
            ).items():
                all_bars.setdefault(
                    symbol, []
                ).extend(bars or [])

            page_token = data.get("next_page_token")

            if not page_token:
                break

    print("Symbols with minute bars:", len(all_bars))
    print(
        "Total minute bars:",
        sum(len(b) for b in all_bars.values())
    )

    return all_bars


def analyze(symbol, snapshot, bars):
    trade = snapshot.get("latestTrade") or {}
    previous = snapshot.get("prevDailyBar") or {}

    price = trade.get("p")
    prev_close = previous.get("c")

    if not price or not prev_close:
        return None, "snapshot_missing"

    if not MIN_PRICE <= price <= MAX_PRICE:
        return None, "price_outside"

    change = (price / prev_close - 1) * 100

    if change < -3 or change > 25:
        return None, "change_outside"

    if not bars:
        return None, "bars_missing"

    now = datetime.now(timezone.utc)

    recent = []
    older = []

    for bar in bars:
        try:
            bar_time = datetime.fromisoformat(
                bar["t"].replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            continue

        age = (now - bar_time).total_seconds()

        if 0 <= age < 300:
            recent.append(bar)
        elif 300 <= age < 600:
            older.append(bar)

    if len(recent) < 2 or len(older) < 2:
        return None, "recent_bars_insufficient"

    recent.sort(key=lambda b: b["t"])

    first_price = recent[0].get("o")
    last_price = recent[-1].get("c")

    if not first_price or not last_price:
        return None, "bar_price_missing"

    five_change = (
        last_price / first_price - 1
    ) * 100

    recent_volume = sum(
        b.get("v") or 0 for b in recent
    )
    older_volume = sum(
        b.get("v") or 0 for b in older
    )

    if older_volume <= 0:
        return None, "volume_missing"

    volume_ratio = recent_volume / older_volume

    if five_change >= 2 and volume_ratio >= 2:
        category = "🔥 강한 초기 급등 신호"
    elif five_change >= 1 and volume_ratio >= 1.5:
        category = "🚀 초기 상승 관심"
    else:
        return None, "conditions_not_met"

    return {
        "symbol": symbol,
        "score": five_change * volume_ratio,
        "message": (
            f"{category}\n"
            f"종목: {symbol}\n"
            f"가격: ${price:.4f}\n"
            f"전일 대비: {change:+.2f}%\n"
            f"최근 5분 상승률: {five_change:+.2f}%\n"
            f"최근 5분 거래량 비율: {volume_ratio:.2f}배\n"
            f"데이터: Alpaca IEX\n"
            f"※ 매수 신호 아님"
        )
    }, "signal"


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

    bars_by_symbol = get_all_bars(symbols)

    signals = []
    reasons = Counter()

    for symbol in symbols:
        result, reason = analyze(
            symbol,
            snapshots.get(symbol) or {},
            bars_by_symbol.get(symbol) or []
        )

        reasons[reason] += 1

        if result:
            signals.append(result)

    print("Analysis diagnostics:")

    for reason, count in sorted(reasons.items()):
        print(f"  {reason}: {count}")

    print("Early signals found:", len(signals))

    if not signals:
        print("초기 급등 조건 충족 신호 없음")
        print("Scan completed")
        return

    signals.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    message = "📡 미국 저가주 초기 급등 탐색\n\n"

    for signal in signals[:3]:
        addition = signal["message"] + "\n\n"

        if len(message + addition) > 1900:
            break

        message += addition

    send_discord(message)
    print("Scan completed")


if __name__ == "__main__":
    main()
