
import os
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

# ================= CONFIG =================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_KEY = os.getenv("TWELVE_DATA_API_KEY", "")
CHAT_ID = os.getenv("CHAT_ID", "")

BASE_URL = "https://api.twelvedata.com"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

SYMBOLS = {
    "XAU/USD": "GOLD",
    "EUR/USD": "EURUSD"
}

NY_TZ = ZoneInfo("America/New_York")
SCAN_INTERVAL = 60

chat_ids = set()
if CHAT_ID:
    chat_ids.add(str(CHAT_ID))

last_signals = {}
last_update_id = 0


# ================= TELEGRAM =================

def send_message(chat_id, message):
    try:
        requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": message
            },
            timeout=20
        )
    except Exception as e:
        print("Telegram error:", e)


def broadcast(message):
    for chat_id in list(chat_ids):
        send_message(chat_id, message)


def get_updates():
    global last_update_id

    try:
        response = requests.get(
            f"{TELEGRAM_URL}/getUpdates",
            params={
                "offset": last_update_id + 1,
                "timeout": 25
            },
            timeout=30
        ).json()

        for update in response.get("result", []):
            last_update_id = update["update_id"]

            msg = update.get("message", {})
            if not msg:
                continue

            chat_id = str(msg["chat"]["id"])
            text = msg.get("text", "").strip().lower()

            chat_ids.add(chat_id)

            if text == "/start":
                send_message(
                    chat_id,
                    "Welcome to ICT FOREX BOT!\n\n"
                    "Markets: GOLD and EURUSD\n"
                    "Timeframes: 15M + 5M\n"
                    "Session: New York\n\n"
                    "Commands:\n"
                    "/scan - Scan market now\n"
                    "/status - Bot status\n"
                )

            elif text == "/status":
                send_message(
                    chat_id,
                    "ICT FOREX BOT is running.\n"
                    "Signal mode: ON\n"
                    "Auto trading: OFF"
                )

            elif text == "/scan":
                send_message(chat_id, "Scanning market...")
                scan_markets()

    except Exception as e:
        print("Update error:", e)


# ================= MARKET DATA =================

def get_candles(symbol, interval):
    response = requests.get(
        f"{BASE_URL}/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": 100,
            "apikey": API_KEY,
            "timezone": "UTC"
        },
        timeout=20
    ).json()

    if "values" not in response:
        raise ValueError(response.get("message", "Data unavailable"))

    # Twelve Data returns newest candle first.
    candles = list(reversed(response["values"]))

    result = []
    for c in candles:
        result.append({
            "open": float(c["open"]),
            "high": float(c["high"]),
            "low": float(c["low"]),
            "close": float(c["close"])
        })

    # Ignore the newest candle because it may still be forming.
    return result[:-1]


# ================= ICT HELPERS =================

def find_swings(candles):
    highs = []
    lows = []

    for i in range(2, len(candles) - 2):
        h = candles[i]["high"]
        l = candles[i]["low"]

        if all(
            h > candles[j]["high"]
            for j in [i-2, i-1, i+1, i+2]
        ):
            highs.append(h)

        if all(
            l < candles[j]["low"]
            for j in [i-2, i-1, i+1, i+2]
        ):
            lows.append(l)

    return highs, lows


def get_htf_bias(candles):
    highs, lows = find_swings(candles)

    if len(highs) < 2 or len(lows) < 2:
        return "NEUTRAL"

    higher_high = highs[-1] > highs[-2]
    higher_low = lows[-1] > lows[-2]

    lower_high = highs[-1] < highs[-2]
    lower_low = lows[-1] < lows[-2]

    if higher_high and higher_low:
        return "BULLISH"

    if lower_high and lower_low:
        return "BEARISH"

    return "NEUTRAL"


def calculate_signal(candles, bias):
    if len(candles) < 30:
        return None

    current = candles[-1]
    previous = candles[-2]
    recent = candles[-21:-1]

    previous_low = min(c["low"] for c in recent)
    previous_high = max(c["high"] for c in recent)

    bodies = [
        abs(c["close"] - c["open"])
        for c in candles[-11:-1]
    ]

    average_body = sum(bodies) / len(bodies)

    body = abs(current["close"] - current["open"])

    bullish_sweep = (
        current["low"] < previous_low
        and current["close"] > previous_low
    )

    bearish_sweep = (
        current["high"] > previous_high
        and current["close"] < previous_high
    )

    bullish_displacement = (
        current["close"] > current["open"]
        and current["close"] > previous["high"]
        and body > average_body * 1.2
    )

    bearish_displacement = (
        current["close"] < current["open"]
        and current["close"] < previous["low"]
        and body > average_body * 1.2
    )

    if (
        bias == "BULLISH"
        and bullish_sweep
        and bullish_displacement
    ):
        entry = current["close"]
        sl = current["low"] - average_body * 0.2
        risk = entry - sl

        if risk <= 0:
            return None

        return {
            "side": "BUY",
            "entry": entry,
            "sl": sl,
            "tp1": entry + risk,
            "tp2": entry + risk * 2,
            "tp3": entry + risk * 3
        }

    if (
        bias == "BEARISH"
        and bearish_sweep
        and bearish_displacement
    ):
        entry = current["close"]
        sl = current["high"] + average_body * 0.2
        risk = sl - entry

        if risk <= 0:
            return None

        return {
            "side": "SELL",
            "entry": entry,
            "sl": sl,
            "tp1": entry - risk,
            "tp2": entry - risk * 2,
            "tp3": entry - risk * 3
        }

    return None


# ================= SESSION FILTER =================

def in_new_york_session():
    now = datetime.now(NY_TZ)

    # Monday-Friday, 08:00 to 11:00 New York time.
    if now.weekday() >= 5:
        return False

    minutes = now.hour * 60 + now.minute
    return 8 * 60 <= minutes < 11 * 60


# ================= SIGNAL ENGINE =================

def format_price(value):
    return f"{value:.5f}"


def scan_markets():
    if not API_KEY:
        print("Missing TWELVE_DATA_API_KEY")
        return

    if not in_new_york_session():
        print("Outside New York session.")
        return

    for symbol, name in SYMBOLS.items():
        try:
            candles_15m = get_candles(symbol, "15min")
            candles_5m = get_candles(symbol, "5min")

            bias = get_htf_bias(candles_15m)
            signal = calculate_signal(candles_5m, bias)

            if not signal:
                print(name, "NO TRADE")
                continue

            # Avoid duplicate alerts for the same side and entry.
            key = (
                symbol,
                signal["side"],
                round(signal["entry"], 5)
            )

            if key in last_signals:
                continue

            last_signals[key] = time.time()

            message = (
                f"📊 ICT FOREX BOT\n\n"
                f"Market: {name}\n"
                f"Signal: {signal['side']}\n"
                f"HTF Bias: {bias}\n"
                f"Entry TF: 5M\n"
                f"Confirmation: 15M\n\n"
                f"Entry: {format_price(signal['entry'])}\n"
                f"Stop Loss: {format_price(signal['sl'])}\n"
                f"TP1: {format_price(signal['tp1'])}\n"
                f"TP2: {format_price(signal['tp2'])}\n"
                f"TP3: {format_price(signal['tp3'])}\n\n"
                f"Risk: Reward = 1:1 / 1:2 / 1:3\n"
                f"Session: New York\n\n"
                f"⚠️ Demo test required. Not financial advice."
            )

            broadcast(message)
            print(name, signal)

        except Exception as e:
            print(name, "Error:", e)


# ================= MAIN =================

def main():
    if not BOT_TOKEN or not API_KEY:
        print("ERROR: Set BOT_TOKEN and TWELVE_DATA_API_KEY.")
        return

    print("ICT FOREX BOT started.")

    while True:
        get_updates()
        scan_markets()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
