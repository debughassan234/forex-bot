"""
Fib Structure Bot v3 — XAU/USD, GBP/USD, EUR/USD on 15min
+ Claude AI double-confirmation before entry signals.

Flow: swings -> Break of Structure (⚡ alert, never missed) ->
price enters 0.382-0.618 golden zone -> Claude reviews the setup ->
signal sent with AI verdict. If the AI check fails/unavailable,
the signal is still sent, marked "unconfirmed".
"""

import os
import time
import json
import requests
from datetime import datetime, timezone

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")

SYMBOLS    = ["XAU/USD", "GBP/USD", "EUR/USD"]
TIMEFRAMES = ["15min"]
CANDLES    = 150
PIVOT_N    = 3
ZONE_LOW   = 0.382
ZONE_HIGH  = 0.618
SL_BUFFER  = 0.10
TP1_EXT    = 0.382
TP2_EXT    = 0.618
FRESH_WINDOW = {"15min": 3, "5min": 3}   # candles counted as "fresh" BOS

HEARTBEAT_UTC_HOUR   = 7    # ~8am Nigeria
HEARTBEAT_WINDOW_MIN = 20


def get_candles(symbol, interval):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={CANDLES}&apikey={TWELVE_DATA_API_KEY}")
    data = requests.get(url, timeout=20).json()
    try:
        vals = data["values"]
        vals.reverse()
        return [{"h": float(v["high"]), "l": float(v["low"]),
                 "c": float(v["close"])} for v in vals]
    except (KeyError, TypeError):
        print(f"[warn] fetch failed {symbol} {interval}: {data.get('message', data)}")
        return None


def find_swings(candles):
    highs, lows = [], []
    for i in range(PIVOT_N, len(candles) - PIVOT_N):
        w = candles[i - PIVOT_N: i + PIVOT_N + 1]
        if candles[i]["h"] == max(c["h"] for c in w):
            highs.append((i, candles[i]["h"]))
        if candles[i]["l"] == min(c["l"] for c in w):
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


def ask_claude(symbol, side, candles, z_bot, z_top, sl, tp1, tp2):
    """Ask Claude to review the setup. Returns (verdict, note)."""
    if not ANTHROPIC_API_KEY:
        return None, "AI check not configured"
    recent = candles[-40:]
    ohlc = " | ".join(f"H{c['h']:.5f} L{c['l']:.5f} C{c['c']:.5f}" for c in recent)
    prompt = (
        f"You are reviewing a {side} setup on {symbol} (15min chart) using a "
        f"break-of-structure + Fibonacci golden zone strategy.\n"
        f"Last 40 candles (oldest to newest): {ohlc}\n"
        f"Proposed: golden zone {z_bot:.5f}-{z_top:.5f}, SL {sl:.5f}, "
        f"TP1 {tp1:.5f}, TP2 {tp2:.5f}.\n"
        f"Judge: is the trend structure clean, is the impulse leg valid, is the "
        f"retracement orderly (not a violent reversal), is the RR sensible?\n"
        f'Respond ONLY with JSON: {{"verdict":"CONFIRM" or "REJECT","reason":"one short sentence"}}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-sonnet-4-6", "max_tokens": 200,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=30)
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        text = text.replace("```json", "").replace("```", "").strip()
        out = json.loads(text)
        return out.get("verdict"), out.get("reason", "")
    except Exception as e:
        print(f"[warn] Claude check failed: {e}")
        return None, "AI check unavailable"


def entry_message(symbol, interval, side, emoji, price, z_bot, z_top, sl, tp1, tp2,
                  verdict, note):
    if verdict == "CONFIRM":
        ai_line = f"🤖 AI check: ✅ CONFIRMED — {note}"
    elif verdict == "REJECT":
        ai_line = f"🤖 AI check: ⚠️ CAUTION — {note}"
    else:
        ai_line = f"🤖 AI check: ❔ unconfirmed ({note})"
    return (
        f"{emoji} *{side} — {symbol} ({interval})*\n\n"
        f"Price `{fmt(symbol, price)}` is IN the golden zone "
        f"`{fmt(symbol, z_bot)}`–`{fmt(symbol, z_top)}`\n\n"
        f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, abs(price - sl))})\n"
        f"TP1 (-0.382): `{fmt(symbol, tp1)}`  ({dist_label(symbol, abs(tp1 - price))})\n"
        f"TP2 (-0.618): `{fmt(symbol, tp2)}`  ({dist_label(symbol, abs(tp2 - price))})\n\n"
        f"{ai_line}\n\n"
        f"_Confirm on your chart before entering. Not financial advice._"
    )


def bos_candle_index(candles, level, direction, start):
    for i in range(start, len(candles)):
        if direction == "up" and candles[i]["c"] > level:
            return i
        if direction == "down" and candles[i]["c"] < level:
            return i
    return None


def analyze(symbol, interval):
    candles = get_candles(symbol, interval)
    if not candles or len(candles) < 40:
        return f"{symbol} {interval}: data unavailable"

    highs, lows = find_swings(candles)
    if len(highs) < 2 or len(lows) < 2:
        return f"{symbol} {interval}: not enough swings"

    price      = candles[-1]["c"]
    prev_close = candles[-2]["c"]
    last_i     = len(candles) - 1
    fresh_n    = FRESH_WINDOW.get(interval, 2)

    (h1_i, h1), (h2_i, h2) = highs[-2], highs[-1]
    (l1_i, l1), (l2_i, l2) = lows[-2],  lows[-1]

    status = "ranging"

    if h2 > h1 and l2 > l1:
        status = "uptrend"
        bos_i = bos_candle_index(candles, h1, "up", h1_i + 1)
        if bos_i is not None:
            leg_lo = l2
            leg_hi = max(c["h"] for c in candles[l2_i:])
            leg = leg_hi - leg_lo
            if leg > 0:
                z_top = leg_hi - ZONE_LOW  * leg
                z_bot = leg_hi - ZONE_HIGH * leg
                sl  = leg_lo - SL_BUFFER * leg
                tp1 = leg_hi + TP1_EXT * leg
                tp2 = leg_hi + TP2_EXT * leg

                if bos_i >= last_i - fresh_n + 1:
                    send_telegram(
                        f"⚡ *BOS UP — {symbol} ({interval})*\n\n"
                        f"Structure broken above `{fmt(symbol, h1)}`\n"
                        f"Impulse leg `{fmt(symbol, leg_lo)}` → `{fmt(symbol, leg_hi)}`\n\n"
                        f"🎯 Golden zone to watch: `{fmt(symbol, z_bot)}` – `{fmt(symbol, z_top)}`\n"
                        f"Planned SL `{fmt(symbol, sl)}` | TP1 `{fmt(symbol, tp1)}` | TP2 `{fmt(symbol, tp2)}`\n\n"
                        f"_Wait for the retracement into the zone._"
                    )
                    status = "uptrend ⚡BOS"

                if z_bot <= price <= z_top and not (z_bot <= prev_close <= z_top):
                    verdict, note = ask_claude(symbol, "BUY", candles, z_bot, z_top, sl, tp1, tp2)
                    send_telegram(entry_message(symbol, interval, "BUY", "🟢", price,
                                                z_bot, z_top, sl, tp1, tp2, verdict, note))
                    status = "uptrend 🟢IN ZONE"

    elif h2 < h1 and l2 < l1:
        status = "downtrend"
        bos_i = bos_candle_index(candles, l1, "down", l1_i + 1)
        if bos_i is not None:
            leg_hi2 = h2
            leg_lo2 = min(c["l"] for c in candles[h2_i:])
            leg = leg_hi2 - leg_lo2
            if leg > 0:
                z_bot = leg_lo2 + ZONE_LOW  * leg
                z_top = leg_lo2 + ZONE_HIGH * leg
                sl  = leg_hi2 + SL_BUFFER * leg
                tp1 = leg_lo2 - TP1_EXT * leg
                tp2 = leg_lo2 - TP2_EXT * leg

                if bos_i >= last_i - fresh_n + 1:
                    send_telegram(
                        f"⚡ *BOS DOWN — {symbol} ({interval})*\n\n"
                        f"Structure broken below `{fmt(symbol, l1)}`\n"
                        f"Impulse leg `{fmt(symbol, leg_hi2)}` → `{fmt(symbol, leg_lo2)}`\n\n"
                        f"🎯 Golden zone to watch: `{fmt(symbol, z_bot)}` – `{fmt(symbol, z_top)}`\n"
                        f"Planned SL `{fmt(symbol, sl)}` | TP1 `{fmt(symbol, tp1)}` | TP2 `{fmt(symbol, tp2)}`\n\n"
                        f"_Wait for the retracement into the zone._"
                    )
                    status = "downtrend ⚡BOS"

                if z_bot <= price <= z_top and not (z_bot <= prev_close <= z_top):
                    verdict, note = ask_claude(symbol, "SELL", candles, z_bot, z_top, sl, tp1, tp2)
                    send_telegram(entry_message(symbol, interval, "SELL", "🔴", price,
                                                z_bot, z_top, sl, tp1, tp2, verdict, note))
                    status = "downtrend 🔴IN ZONE"

    line = f"{symbol} {interval}: {fmt(symbol, price)} — {status}"
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
                      f"\n\n_{now.strftime('%a %d %b, %H:%M')} UTC · 15min chart · checks every ~5 min_")


if __name__ == "__main__":
    main()
