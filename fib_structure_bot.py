"""
Fib Structure Bot — XAU/USD, GBP/USD, EUR/USD
Strategy: swing structure (HH/HL/LH/LL) + break of structure +
retracement into the 0.382-0.618 golden zone.
SL beyond the 1.0 level | TP1 at -0.382 | TP2 at -0.618 extension
"""

import os
import requests

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS   = ["XAU/USD", "GBP/USD", "EUR/USD"]
INTERVAL  = "30min"
CANDLES   = 120
PIVOT_N   = 3
ZONE_LOW  = 0.382
ZONE_HIGH = 0.618
SL_BUFFER = 0.10
TP1_EXT   = -0.382
TP2_EXT   = -0.618


def get_candles(symbol):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={INTERVAL}&outputsize={CANDLES}&apikey={TWELVE_DATA_API_KEY}")
    data = requests.get(url, timeout=20).json()
    try:
        vals = data["values"]
        vals.reverse()
        return [{"h": float(v["high"]), "l": float(v["low"]),
                 "c": float(v["close"]), "t": v["datetime"]} for v in vals]
    except (KeyError, TypeError):
        print(f"[warn] candles fetch failed for {symbol}: {data.get('message', data)}")
        return None


def find_swings(candles):
    highs, lows = [], []
    for i in range(PIVOT_N, len(candles) - PIVOT_N):
        window = candles[i - PIVOT_N: i + PIVOT_N + 1]
        if candles[i]["h"] == max(c["h"] for c in window):
            highs.append((i, candles[i]["h"]))
        if candles[i]["l"] == min(c["l"] for c in window):
            lows.append((i, candles[i]["l"]))
    return highs, lows


def fmt(symbol, x):
    return f"{x:.2f}" if "XAU" in symbol else f"{x:.5f}"


def dist_label(symbol, d):
    return f"${d:.2f}" if "XAU" in symbol else f"{d / 0.0001:.0f} pips"


def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                        "parse_mode": "Markdown"}, timeout=15)


def analyze(symbol):
    candles = get_candles(symbol)
    if not candles or len(candles) < 40:
        return

    highs, lows = find_swings(candles)
    if len(highs) < 2 or len(lows) < 2:
        return

    price      = candles[-1]["c"]
    prev_close = candles[-2]["c"]
    (h1_i, h1), (h2_i, h2) = highs[-2], highs[-1]
    (l1_i, l1), (l2_i, l2) = lows[-2],  lows[-1]

    setup = None

    if h2 > h1 and l2 > l1 and max(c["h"] for c in candles[h1_i:]) > h1:
        leg_lo, leg_hi = l2, max(c["h"] for c in candles[l2_i:])
        leg = leg_hi - leg_lo
        if leg > 0:
            z_top = leg_hi - ZONE_LOW  * leg
            z_bot = leg_hi - ZONE_HIGH * leg
            in_zone_now  = z_bot <= price <= z_top
            was_outside  = not (z_bot <= prev_close <= z_top)
            if in_zone_now and was_outside:
                sl  = leg_lo - SL_BUFFER * leg
                tp1 = leg_hi - TP1_EXT * leg
                tp2 = leg_hi - TP2_EXT * leg
                setup = ("BUY", "🟢", z_bot, z_top, sl, tp1, tp2,
                         "Uptrend (HH+HL), structure broken up, price retraced into the golden zone")

    if setup is None and h2 < h1 and l2 < l1 and min(c["l"] for c in candles[l1_i:]) < l1:
        leg_hi2 = h2
        leg_lo2 = min(c["l"] for c in candles[h2_i:])
        leg = leg_hi2 - leg_lo2
        if leg > 0:
            z_bot = leg_lo2 + ZONE_LOW  * leg
            z_top = leg_lo2 + ZONE_HIGH * leg
            in_zone_now = z_bot <= price <= z_top
            was_outside = not (z_bot <= prev_close <= z_top)
            if in_zone_now and was_outside:
                sl  = leg_hi2 + SL_BUFFER * leg
                tp1 = leg_lo2 - abs(TP1_EXT) * leg
                tp2 = leg_lo2 - abs(TP2_EXT) * leg
                setup = ("SELL", "🔴", z_bot, z_top, sl, tp1, tp2,
                         "Downtrend (LH+LL), structure broken down, price retraced into the golden zone")

    print(f"{symbol}: price={fmt(symbol, price)} -> {setup[0] if setup else 'no setup'}")

    if setup:
        side, emoji, z_bot, z_top, sl, tp1, tp2, reason = setup
        send_telegram(
            f"{emoji} *{side} setup — {symbol}*\n\n"
            f"Golden zone: `{fmt(symbol, z_bot)}` – `{fmt(symbol, z_top)}`\n"
            f"Current price: `{fmt(symbol, price)}`\n\n"
            f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, abs(price - sl))})\n"
            f"TP1 (-0.382): `{fmt(symbol, tp1)}`  ({dist_label(symbol, abs(tp1 - price))})\n"
            f"TP2 (-0.618): `{fmt(symbol, tp2)}`  ({dist_label(symbol, abs(tp2 - price))})\n\n"
            f"Why: {reason}\n"
            f"Timeframe: {INTERVAL}\n\n"
            f"_Check the chart and confirm the structure before entering. Not financial advice._"
        )


def main():
    for s in SYMBOLS:
        try:
            analyze(s)
        except Exception as e:
            print(f"[error] {s}: {e}")


if __name__ == "__main__":
    main()
