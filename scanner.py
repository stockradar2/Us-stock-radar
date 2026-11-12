
import os
import json
import time
from pathlib import Path
from collections import Counter
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
COOLDOWN = 3600

session = requests.Session()
session.headers.update(HEADERS)


def api_get(base, path, params=None):
    response = session.get(
        base + path,
        params=params,
        timeout=35
    )
    response.raise_for_status()
    return response.json()


def send_discord(message):
    if not DISCORD:
        print("Discord webhook missing")
        return False

    response = requests.post(
        DISCORD,
        json={"content": message},
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
        "ETF", "ETN", "EXCHANGE TRADED",
        "EXCHANGE-TRADED", "LEVERAGED",
        "INVERSE", "2X ", "3X ",
        "PROSHARES", "DIREXION",
        "GRANITESHARES", "T-REX",
        "DEFIANCE", "ROUNDHILL"
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

    for offset in range(0, len(symbols), 100):
        batch = symbols[offset:offset + 100]

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


def get_bars(symbols):
    
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(hours=24)
    print("Bars query UTC:", start.isoformat(), end.isoformat())
    print("Current UTC:", datetime.now(timezone.utc).isoformat())
    print("Requested feed:", "sip")
    print("UTC now:", end.isoformat())
    print("Bars start:", start.isoformat())
    print("Bars end:", end.isoformat())
    result = {}

    for offset in range(0, len(symbols), 50):
        batch = symbols[offset:offset + 50]
        token = None

        while True:
            params = {
                "symbols": ",".join(batch),
                "timeframe": "1Min",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "feed": "sip"
            }

            if token:
                params["page_token"] = token

            data = api_get(
                DATA_URL,
                "/v2/stocks/bars",
                params
            )
            print("Bars API response:", str(data)[:500])
            for symbol, bars in (
                data.get("bars") or {}
            ).items():
                result.setdefault(
                    symbol, []
                ).extend(bars or [])

            token = data.get("next_page_token")
            if not token:
                break

    print("Symbols with bars:", len(result))
    print(
        "Total minute bars:",
        sum(len(x) for x in result.values())
    )
    return result


def analyze(symbol, snapshot, bars, now):
    trade = snapshot.get("latestTrade") or {}
    previous = snapshot.get("prevDailyBar") or {}

    price = trade.get("p")
    prev_close = previous.get("c")

    if not price or not prev_close:
        return None, "snapshot_missing"

    if not MIN_PRICE <= price <= MAX_PRICE:
        return None, "price_outside"

    day_change = (price / prev_close - 1) * 100

    # 이미 크게 오른 종목은 초기 신호에서 제외
    if day_change < -30 or day_change > 100:
        return None, "change_outside"

    if not bars:
        return None, "bars_missing"

    recent = []
    older = []

    for bar in bars:
        try:
            stamp = datetime.fromisoformat(
                bar["t"].replace("Z", "+00:00")
            )
        except (KeyError, TypeError, ValueError):
            continue

        age = (now - stamp).total_seconds()
        if age >= 600:
            print(f"{symbol}: old bar age={age:.0f}s")
        if age < 600:
            print(f"{symbol}: bar age={age:.0f}s")
        if 0 <= age < 300:
            recent.append(bar)
        elif 300 <= age < 600:
            older.append(bar)

    if not recent or not older:
        return None, "recent_bars_insufficient"

    recent.sort(key=lambda x: x["t"])
    older.sort(key=lambda x: x["t"])

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

    # 최소 거래량 기준
    if recent_volume < 1000:
        return None, "low_volume"

    
    if five_change >= 1.5 and volume_ratio >= 2:
        category = "🔥 강한 초기 급등 관심"
        priority = 3

    elif five_change >= 0.5 and volume_ratio >= 1.5:
        category = "🚀 초기 상승 관심"
        priority = 2

    elif five_change >= 0 and volume_ratio >= 2:
        category = "👀 거래량 선행 관심"
        priority = 1

    else:
        return None, "conditions_not_met"


    score = (
        priority * 100
        + five_change * 5
        + min(volume_ratio, 10)
    )

    message = (
        f"{category}\n"
        f"종목: {symbol}\n"
        f"가격: ${price:.4f}\n"
        f"전일 대비: {day_change:+.2f}%\n"
        f"최근 5분 상승률: {five_change:+.2f}%\n"
        f"5분 거래량 비교: {volume_ratio:.2f}배\n"
        f"최근 구간 거래량: {recent_volume:,}\n"
        f"데이터: Alpaca IEX\n"
        f"※ 매수 신호 아님"
    )

    return {
        "symbol": symbol,
        "score": score,
        "message": message
    }, "signal"


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
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
        raise RuntimeError("Alpaca keys missing")

    symbols = discover_symbols()
    if not symbols:
        print("No candidates")
        return

    symbols = filter_stocks(symbols)
    if not symbols:
        print("No filtered candidates")
        return

    snapshots = get_snapshots(symbols)

    # 가격 조건을 통과한 종목만 분봉 조회
    eligible = []

    for symbol in symbols:
        trade = (
            snapshots.get(symbol) or {}
        ).get("latestTrade") or {}

        price = trade.get("p")

        if price and MIN_PRICE <= price <= MAX_PRICE:
            eligible.append(symbol)

    print("Price-eligible symbols:", len(eligible))

    if not eligible:
        print("Scan completed")
        return

    bars_by_symbol = get_bars(eligible)

    now = datetime.now(timezone.utc)
    reasons = Counter()
    signals = []

    for symbol in eligible:
        signal, reason = analyze(
            symbol,
            snapshots.get(symbol) or {},
            bars_by_symbol.get(symbol) or [],
            now
        )

        reasons[reason] += 1

        if signal:
            signals.append(signal)

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

    state = load_state()
    current_time = time.time()
    selected = []

    for signal in signals:
        symbol = signal["symbol"]
        last = state.get(symbol, 0)

        if current_time - last < COOLDOWN:
            continue

        selected.append(signal)

        if len(selected) >= 3:
            break

    print("Signals after cooldown:", len(selected))

    if not selected:
        print("All signals in cooldown")
        return

    message = "📡 미국 저가주 초기 급등 탐색\n\n"
    included = []

    for signal in selected:
        addition = signal["message"] + "\n\n"

        if len(message + addition) > 1900:
            break

        message += addition
        included.append(signal)

    if included and send_discord(message):
        for signal in included:
            state[signal["symbol"]] = current_time

        save_state(state)

    print("Scan completed")


if __name__ == "__main__":
    main()
