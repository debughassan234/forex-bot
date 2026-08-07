"""
Fib Structure Bot v5.1 — XAU/USD, GBP/USD, EUR/USD on 15min
TWO-STAGE SIGNALS for fast entries:
  Stage 1  ⚡ BOS heads-up  — structure just broke (continuation or CHoCH);
           fib zone drawn; get to your chart.
  Stage 2  🟢/🔴 ENTER NOW — price retraced into the 0.382-0.618 zone AND a
           confirmation candle (engulfing/pin bar) closed. Enter immediately.
SL beyond the 1.0 | TP1 -0.382 | TP2 -0.618. Heartbeat ~8am WAT.
"""

import os
import time
import requests
from datetime import datetime, timezone

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS    = ["XAU/USD", "GBP/USD", "EUR/USD"]
TIMEFRAMES = ["15min"]
CANDLES    = 150
PIVOT_N    = 3
ZONE_LOW   = 0.382
ZONE_HIGH  = 0.618
SL_BUFFER  = 0.10
TP1_EXT    = 0.382
TP2_EXT    = 0.618

BOS_FRESH_CANDLES  = 2    # BOS alert only if the break happened in the last 2 candles
MAX_CANDLE_AGE_MIN = 6    # entry signal only right after the confirming candle closes

HEARTBEAT_UTC_HOUR   = 7
HEARTBEAT_WINDOW_MIN = 20


def get_candles(symbol, interval):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={CANDLES}&apikey={TWELVE_DATA_API_KEY}")
    data = requests.get(url, timeout=20).json()
    try:
        vals = data["values"]
        vals.reverse()
        return [{"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
                 "l": float(v["low"]), "c": float(v["close"])} for v in vals]
    except (KeyError, TypeError):
        print(f"[warn] fetch failed {symbol} {interval}: {data.get('message', data)}")
        return None


def latest_candle_age_minutes(candles):
    try:
        t = datetime.strptime(candles[-1]["t"], "%Y-%m-%d %H:%M:%S")
        t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return 0.0


def find_swings(candles):
    highs, lows = [], []
    for i in range(PIVOT_N, len(candles) - PIVOT_N):
        w = candles[i - PIVOT_N: i + PIVOT_N + 1]
        if candles[i]["h"] == max(c["h"] for c in w):
            highs.append((i, candles[i]["h"]))
        if candles[i]["l"] == min(c["l"] for c in w):
            lows.append((i, candles[i]["l"]))
    return highs, lows


def fmt(s, x):
    return f"{x:.2f}" if "XAU" in s else f"{x:.5f}"


def dist_label(s, d):
    return f"${d:.2f}" if "XAU" in s else f"{d / 0.0001:.0f} pips"


def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                        "parse_mode": "Markdown"}, timeout=15)


def confirmation(a, b, direction):
    body = abs(b["c"] - b["o"])
    rng  = b["h"] - b["l"]
    if rng == 0:
        return None
    upper = b["h"] - max(b["c"], b["o"])
    lower = min(b["c"], b["o"]) - b["l"]
    if direction == "bull":
        if b["c"] > b["o"] and a["c"] < a["o"] and b["c"] > a["o"] and b["o"] < a["c"]:
            return "bullish engulfing"
        if lower > 2 * body and lower > 0.5 * rng:
            return "bullish pin bar"
    else:
        if b["c"] < b["o"] and a["c"] > a["o"] and b["c"] < a["o"] and b["o"] > a["c"]:
            return "bearish engulfing"
        if upper > 2 * body and upper > 0.5 * rng:
            return "bearish pin bar"
    return None


def first_close_beyond(candles, level, direction, start):
    for i in range(start, len(candles)):
        if direction == "up" and candles[i]["c"] > level:
            return i
        if direction == "down" and candles[i]["c"] < level:
            return i
    return None


def process(symbol, interval, direction, leg_hi, leg_lo, candles, kind, bos_i):
    leg = leg_hi - leg_lo
    if leg <= 0:
        return ""
    if direction == "buy":
        z_top = leg_hi - ZONE_LOW  * leg
        z_bot = leg_hi - ZONE_HIGH * leg
        sl  = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
        want, side, emoji, arrow = "bull", "BUY", "🟢", "UP"
    else:
        z_bot = leg_lo + ZONE_LOW  * leg
        z_top = leg_lo + ZONE_HIGH * leg
        sl  = leg_hi + SL_BUFFER * leg
        tp1 = leg_lo - TP1_EXT * leg
        tp2 = leg_lo - TP2_EXT * leg
        want, side, emoji, arrow = "bear", "SELL", "🔴", "DOWN"

    label = "TREND CHANGE (CHoCH)" if "reversal" in kind else "trend continuation"
    last_i = len(candles) - 1
    price  = candles[-1]["c"]

    # -------- Stage 1: fresh BOS heads-up --------
    if bos_i is not None and bos_i >= last_i - BOS_FRESH_CANDLES + 1:
        send_telegram(
            f"⚡ *BOS {arrow} — {symbol} ({interval})* — {label}\n\n"
            f"Fib leg `{fmt(symbol, leg_lo if direction=='buy' else leg_hi)}` → "
            f"`{fmt(symbol, leg_hi if direction=='buy' else leg_lo)}`\n"
            f"🎯 Golden zone: `{fmt(symbol, z_bot)}` – `{fmt(symbol, z_top)}`\n"
            f"Planned SL `{fmt(symbol, sl)}` | TP1 `{fmt(symbol, tp1)}` | TP2 `{fmt(symbol, tp2)}`\n\n"
            f"_Get ready — waiting for retrace + confirmation candle._"
        )

    # -------- Stage 2: confirmed entry --------
    b, a = candles[-2], candles[-3]
    conf = confirmation(a, b, want)
    touched_zone = (b["l"] <= z_top and b["h"] >= z_bot)
    fresh = latest_candle_age_minutes(candles) <= MAX_CANDLE_AGE_MIN

    if conf and touched_zone and fresh:
        send_telegram(
            f"{emoji} *{side} NOW — {symbol} ({interval})* — {label}\n\n"
            f"Retrace into `{fmt(symbol, z_bot)}`–`{fmt(symbol, z_top)}` "
            f"CONFIRMED by *{conf}*\n"
            f"Current price: `{fmt(symbol, price)}`\n\n"
            f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, abs(price - sl))})\n"
            f"TP1 (-0.382): `{fmt(symbol, tp1)}`  ({dist_label(symbol, abs(tp1 - price))})\n"
            f"TP2 (-0.618): `{fmt(symbol, tp2)}`  ({dist_label(symbol, abs(tp2 - price))})\n\n"
            f"_Rule-based signal — glance at your chart, manage risk. Not financial advice._"
        )
        return f"{emoji}{side} CONFIRMED"
    if touched_zone:
        return "in zone, awaiting confirmation"
    return "zone set, awaiting retrace"


def analyze(symbol, interval):
    candles = get_candles(symbol, interval)
    if not candles or len(candles) < 40:
        return f"{symbol} {interval}: data unavailable"

    highs, lows = find_swings(candles)
    if len(highs) < 2 or len(lows) < 2:
        return f"{symbol} {interval}: not enough swings"

    (h1_i, h1), (h2_i, h2) = highs[-2], highs[-1]
    (l1_i, l1), (l2_i, l2) = lows[-2],  lows[-1]
    price = candles[-1]["c"]

    status, note = "ranging", ""

    if h2 > h1 and l2 > l1:
        status = "uptrend"
        rev_i = first_close_beyond(candles, l2, "down", l2_i + 1)
        if rev_i is not None:
            new_low = min(c["l"] for c in candles[h2_i:])
            note = process(symbol, interval, "sell", h2, new_low, candles, "reversal-down", rev_i)
        else:
            cont_i = first_close_beyond(candles, h1, "up", h1_i + 1)
            if cont_i is not None:
                leg_hi = max(c["h"] for c in candles[l2_i:])
                note = process(symbol, interval, "buy", leg_hi, l2, candles, "continuation-up", cont_i)

    elif h2 < h1 and l2 < l1:
        status = "downtrend"
        rev_i = first_close_beyond(candles, h2, "up", h2_i + 1)
        if rev_i is not None:
            new_high = max(c["h"] for c in candles[l2_i:])
            note = process(symbol, interval, "buy", new_high, l2, candles, "reversal-up", rev_i)
        else:
            cont_i = first_close_beyond(candles, l1, "down", l1_i + 1)
            if cont_i is not None:
                leg_lo = min(c["l"] for c in candles[h2_i:])
                note = process(symbol, interval, "sell", h2, leg_lo, candles, "continuation-down", cont_i)

    line = f"{symbol} {interval}: {fmt(symbol, price)} — {status}" + (f" | {note}" if note else "")
    print(line)
    return line


def main():
    statuses = []
    for s in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                statuses.append(analyze(s, tf))
            except Exception as e:
                print(f"[error] {s} {tf}: {e}")
                statuses.append(f"{s} {tf}: error")
            time.sleep(8)

    now = datetime.now(timezone.utc)
    if now.hour == HEARTBEAT_UTC_HOUR and now.minute < HEARTBEAT_WINDOW_MIN:
        send_telegram("✅ *Daily heartbeat — bot is alive*\n\n" +
                      "\n".join(statuses) +
                      f"\n\n_{now.strftime('%a %d %b, %H:%M')} UTC · 15min · two-stage signals_")


if __name__ == "__main__":
    main()
