"""
Fib Structure Bot v4 — XAU/USD, GBP/USD, EUR/USD on 15min
Break of Structure exactly as traded from the chart:
  UPTREND (HH+HL):
    - price closes BELOW the last HL  -> ⚡ BOS DOWN (trend change)
      fib drawn on the down leg (last HH -> new low), sell the retrace
    - price closes ABOVE the last HH  -> ⚡ BOS UP (continuation)
      fib drawn on the up leg, buy the retrace
  DOWNTREND (LH+LL): mirrored.
Entry alert when price is in the 0.382-0.618 golden zone, including
candlestick confirmation status (engulfing / pin bar) on the latest candle.
SL beyond the 1.0 | TP1 at -0.382 | TP2 at -0.618. Daily heartbeat ~8am WAT.
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
FRESH_N    = 3          # how many recent candles count as a "fresh" break

HEARTBEAT_UTC_HOUR   = 7
HEARTBEAT_WINDOW_MIN = 20


def get_candles(symbol, interval):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={CANDLES}&apikey={TWELVE_DATA_API_KEY}")
    data = requests.get(url, timeout=20).json()
    try:
        vals = data["values"]
        vals.reverse()
        return [{"o": float(v["open"]), "h": float(v["high"]),
                 "l": float(v["low"]),  "c": float(v["close"])} for v in vals]
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


def fmt(s, x):
    return f"{x:.2f}" if "XAU" in s else f"{x:.5f}"


def dist_label(s, d):
    return f"${d:.2f}" if "XAU" in s else f"{d / 0.0001:.0f} pips"


def send_telegram(msg):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                  json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                        "parse_mode": "Markdown"}, timeout=15)


def candle_confirmation(candles, direction):
    """Check the last CLOSED candle for engulfing or pin-bar confirmation."""
    a, b = candles[-3], candles[-2]          # b = last closed candle
    body = abs(b["c"] - b["o"])
    rng  = b["h"] - b["l"]
    if rng == 0:
        return None
    upper_wick = b["h"] - max(b["c"], b["o"])
    lower_wick = min(b["c"], b["o"]) - b["l"]

    if direction == "bull":
        if b["c"] > b["o"] and a["c"] < a["o"] and b["c"] > a["o"] and b["o"] < a["c"]:
            return "bullish engulfing"
        if lower_wick > 2 * body and lower_wick > 0.5 * rng:
            return "bullish pin bar (rejection wick)"
    else:
        if b["c"] < b["o"] and a["c"] > a["o"] and b["c"] < a["o"] and b["o"] > a["c"]:
            return "bearish engulfing"
        if upper_wick > 2 * body and upper_wick > 0.5 * rng:
            return "bearish pin bar (rejection wick)"
    return None


def first_close_beyond(candles, level, direction, start):
    for i in range(start, len(candles)):
        if direction == "up" and candles[i]["c"] > level:
            return i
        if direction == "down" and candles[i]["c"] < level:
            return i
    return None


def bos_alert(symbol, interval, kind, broken_level, leg_a, leg_b, z_bot, z_top,
              sl, tp1, tp2):
    arrow = "UP" if kind in ("continuation-up", "reversal-up") else "DOWN"
    label = "trend continuation" if "continuation" in kind else "TREND CHANGE (CHoCH)"
    send_telegram(
        f"⚡ *BOS {arrow} — {symbol} ({interval})* — {label}\n\n"
        f"Structure broken at `{fmt(symbol, broken_level)}`\n"
        f"Fib drawn on leg `{fmt(symbol, leg_a)}` → `{fmt(symbol, leg_b)}`\n\n"
        f"🎯 Golden zone to watch: `{fmt(symbol, z_bot)}` – `{fmt(symbol, z_top)}`\n"
        f"Planned SL `{fmt(symbol, sl)}` | TP1 `{fmt(symbol, tp1)}` | TP2 `{fmt(symbol, tp2)}`\n\n"
        f"_Wait for the retracement into the zone._"
    )


def entry_alert(symbol, interval, side, emoji, price, z_bot, z_top, sl, tp1, tp2, confirm):
    conf_line = (f"🕯 Candle confirmation: ✅ {confirm}" if confirm
                 else "🕯 Candle confirmation: ⏳ not yet — consider waiting for an engulfing/pin bar")
    send_telegram(
        f"{emoji} *{side} — {symbol} ({interval})*\n\n"
        f"Price `{fmt(symbol, price)}` is IN the golden zone "
        f"`{fmt(symbol, z_bot)}`–`{fmt(symbol, z_top)}`\n\n"
        f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, abs(price - sl))})\n"
        f"TP1 (-0.382): `{fmt(symbol, tp1)}`  ({dist_label(symbol, abs(tp1 - price))})\n"
        f"TP2 (-0.618): `{fmt(symbol, tp2)}`  ({dist_label(symbol, abs(tp2 - price))})\n"
        f"{conf_line}\n\n"
        f"_Confirm structure on your chart before entering. Not financial advice._"
    )


def setup_from_leg(symbol, interval, direction, leg_hi, leg_lo, candles,
                   price, prev_close, bos_i, last_i, kind, broken_level):
    """Shared zone/SL/TP math + alerts for any BOS leg."""
    leg = leg_hi - leg_lo
    if leg <= 0:
        return None
    if direction == "buy":
        z_top = leg_hi - ZONE_LOW  * leg
        z_bot = leg_hi - ZONE_HIGH * leg
        sl  = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
    else:
        z_bot = leg_lo + ZONE_LOW  * leg
        z_top = leg_lo + ZONE_HIGH * leg
        sl  = leg_hi + SL_BUFFER * leg
        tp1 = leg_lo - TP1_EXT * leg
        tp2 = leg_lo - TP2_EXT * leg

    tag = ""
    if bos_i is not None and bos_i >= last_i - FRESH_N + 1:
        leg_a, leg_b = (leg_lo, leg_hi) if direction == "buy" else (leg_hi, leg_lo)
        bos_alert(symbol, interval, kind, broken_level, leg_a, leg_b,
                  z_bot, z_top, sl, tp1, tp2)
        tag = "⚡BOS"

    if z_bot <= price <= z_top and not (z_bot <= prev_close <= z_top):
        conf = candle_confirmation(candles, "bull" if direction == "buy" else "bear")
        if direction == "buy":
            entry_alert(symbol, interval, "BUY", "🟢", price, z_bot, z_top, sl, tp1, tp2, conf)
            tag = "🟢IN ZONE"
        else:
            entry_alert(symbol, interval, "SELL", "🔴", price, z_bot, z_top, sl, tp1, tp2, conf)
            tag = "🔴IN ZONE"
    return tag


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

    (h1_i, h1), (h2_i, h2) = highs[-2], highs[-1]
    (l1_i, l1), (l2_i, l2) = lows[-2],  lows[-1]

    status, tag = "ranging", ""

    # ---------------- UPTREND: HH + HL ----------------
    if h2 > h1 and l2 > l1:
        status = "uptrend"
        # Reversal BOS (your chart): close BELOW the last HL
        rev_i = first_close_beyond(candles, l2, "down", l2_i + 1)
        if rev_i is not None:
            new_low = min(c["l"] for c in candles[h2_i:])
            t = setup_from_leg(symbol, interval, "sell", h2, new_low, candles,
                               price, prev_close, rev_i, last_i,
                               "reversal-down", l2)
            if t: tag = t + " (CHoCH)"
        else:
            # Continuation BOS: close ABOVE the last HH
            cont_i = first_close_beyond(candles, h1, "up", h1_i + 1)
            if cont_i is not None:
                leg_hi = max(c["h"] for c in candles[l2_i:])
                t = setup_from_leg(symbol, interval, "buy", leg_hi, l2, candles,
                                   price, prev_close, cont_i, last_i,
                                   "continuation-up", h1)
                if t: tag = t

    # ---------------- DOWNTREND: LH + LL ----------------
    elif h2 < h1 and l2 < l1:
        status = "downtrend"
        # Reversal BOS: close ABOVE the last LH
        rev_i = first_close_beyond(candles, h2, "up", h2_i + 1)
        if rev_i is not None:
            new_high = max(c["h"] for c in candles[l2_i:])
            t = setup_from_leg(symbol, interval, "buy", new_high, l2, candles,
                               price, prev_close, rev_i, last_i,
                               "reversal-up", h2)
            if t: tag = t + " (CHoCH)"
        else:
            cont_i = first_close_beyond(candles, l1, "down", l1_i + 1)
            if cont_i is not None:
                leg_lo = min(c["l"] for c in candles[h2_i:])
                t = setup_from_leg(symbol, interval, "sell", h2, leg_lo, candles,
                                   price, prev_close, cont_i, last_i,
                                   "continuation-down", l1)
                if t: tag = t

    line = f"{symbol} {interval}: {fmt(symbol, price)} — {status} {tag}".strip()
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
                      f"\n\n_{now.strftime('%a %d %b, %H:%M')} UTC · 15min · every ~5 min_")


if __name__ == "__main__":
    main()
