"""
Fib Structure Bot v7.1 — MULTI-TIMEFRAME (Daily -> 4H -> 1H)
XAU/USD, EUR/USD, USD/JPY, GBP/JPY

v7.1 fixes: entry must be INSIDE the zone, minimum RR gate,
and memory so the same setup is never signalled twice.
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN  = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID    = os.environ["TELEGRAM_CHAT_ID"]

SYMBOLS   = ["XAU/USD", "EUR/USD", "USD/JPY", "GBP/JPY"]

TF_BIAS   = "1day"
TF_ZONE   = "4h"
TF_ENTRY  = "1h"

N_BIAS, N_ZONE, N_ENTRY = 120, 180, 180
PIVOT_BIAS, PIVOT_ZONE, PIVOT_ENTRY = 3, 3, 2

ZONE_LOW    = 0.382
ZONE_HIGH   = 0.618
ZONE_PRIME  = 0.5
SL_BUFFER   = 0.10
TP1_EXT     = 0.382
TP2_EXT     = 0.618

MIN_LEG_ATR    = 1.5
ZONE_LOOKBACK  = 12
ENTRY_MAX_AGE  = 15    # minutes: only the run right after a 1H candle closes
MIN_RR         = 1.5   # reject any setup whose TP1 reward:risk is below this
ZONE_TOL       = 0.10  # entry must be inside the zone (10% of zone width slack)
STATE_FILE     = "bot_state.json"
COOLDOWN_HOURS = 12    # do not re-signal the same symbol+direction inside this

# ------- risk / position sizing (edit these to match your account) -------
ACCOUNT_SIZE   = 100000.0
RISK_PERCENT   = 0.5
DAILY_LOSS_CAP = 5.0
MAX_LOTS       = 5.0

CONTRACT   = {"XAU/USD": 100.0, "EUR/USD": 100000.0,
              "USD/JPY": 100000.0, "GBP/JPY": 100000.0}
JPY_QUOTED = {"USD/JPY", "GBP/JPY"}
PIP        = {"EUR/USD": 0.0001, "USD/JPY": 0.01, "GBP/JPY": 0.01}
MA_PERIOD  = 50

USDJPY_RATE = None

HEARTBEAT_UTC_HOUR   = 7
HEARTBEAT_WINDOW_MIN = 25


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=1)
    except Exception as e:
        print(f"[warn] could not save state: {e}")


def already_sent(state, symbol, bias, zone):
    rec = state.get(f"{symbol}|{bias}")
    if not rec:
        return False
    try:
        when = datetime.fromisoformat(rec["t"])
    except Exception:
        return False
    if (datetime.now(timezone.utc) - when) > timedelta(hours=COOLDOWN_HOURS):
        return False
    width = max(zone["z_top"] - zone["z_bot"], 1e-9)
    return abs(rec.get("z_bot", 0) - zone["z_bot"]) < 0.1 * width


def mark_sent(state, symbol, bias, zone):
    state[f"{symbol}|{bias}"] = {"t": datetime.now(timezone.utc).isoformat(),
                                 "z_bot": zone["z_bot"], "z_top": zone["z_top"]}


def get_candles(symbol, interval, size):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={size}&apikey={TWELVE_DATA_API_KEY}")
    data = requests.get(url, timeout=25).json()
    try:
        vals = data["values"]
        vals.reverse()
        return [{"t": v["datetime"], "o": float(v["open"]), "h": float(v["high"]),
                 "l": float(v["low"]), "c": float(v["close"])} for v in vals]
    except (KeyError, TypeError):
        print(f"[warn] {symbol} {interval}: {data.get('message', data)}")
        return None


def find_swings(candles, pivot):
    highs, lows = [], []
    for i in range(pivot, len(candles) - pivot):
        w = candles[i - pivot: i + pivot + 1]
        if candles[i]["h"] == max(c["h"] for c in w):
            highs.append((i, candles[i]["h"]))
        if candles[i]["l"] == min(c["l"] for c in w):
            lows.append((i, candles[i]["l"]))
    return highs, lows


def atr(candles, period=14):
    trs = []
    for i in range(1, len(candles)):
        p, c = candles[i - 1], candles[i]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    if not trs:
        return 0.0
    return sum(trs[-period:]) / min(period, len(trs))


def fmt(sym, x):
    if "XAU" in sym:
        return f"{x:,.2f}"
    if sym in JPY_QUOTED:
        return f"{x:.3f}"
    return f"{x:.5f}"


def dist_label(sym, d):
    if "XAU" in sym:
        return f"${d:,.2f}"
    return f"{d / PIP.get(sym, 0.0001):.0f} pips"


def candle_age_minutes(candles):
    try:
        t = datetime.strptime(candles[-1]["t"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 60.0
    except Exception:
        return 0.0


def send_telegram(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                            "parse_mode": "Markdown"}, timeout=20)
    except Exception as e:
        print(f"[warn] telegram send failed: {e}")


def lot_size(symbol, sl_distance, price):
    if sl_distance <= 0 or price <= 0:
        return 0.0, 0.0
    risk_usd = ACCOUNT_SIZE * (RISK_PERCENT / 100.0)
    per_lot_loss = sl_distance * CONTRACT.get(symbol, 100000.0)
    if symbol in JPY_QUOTED:
        rate = price if symbol == "USD/JPY" else USDJPY_RATE
        if not rate:
            return 0.0, risk_usd
        per_lot_loss = per_lot_loss / rate
    if per_lot_loss <= 0:
        return 0.0, risk_usd
    return min(round(risk_usd / per_lot_loss, 2), MAX_LOTS), risk_usd


def sma(candles, period):
    closes = [c["c"] for c in candles]
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def three_swing_aligned(highs, lows, direction):
    if len(highs) < 3 or len(lows) < 3:
        return False
    h = [p for _, p in highs[-3:]]
    l = [p for _, p in lows[-3:]]
    if direction == "bullish":
        return h[0] < h[1] < h[2] and l[0] < l[1] < l[2]
    return h[0] > h[1] > h[2] and l[0] > l[1] > l[2]


def daily_bias(candles, pivot=None):
    pivot = PIVOT_BIAS if pivot is None else pivot
    highs, lows = find_swings(candles, pivot)

    established, inval_level, inval_i = None, None, None
    n = min(len(highs), len(lows))
    for k in range(1, n):
        hp, hc = highs[-k - 1][1], highs[-k][1]
        lp, lc = lows[-k - 1][1],  lows[-k][1]
        if hc > hp and lc > lp:
            established, inval_level, inval_i = "bullish", lows[-k][1], lows[-k][0]
            break
        if hc < hp and lc < lp:
            established, inval_level, inval_i = "bearish", highs[-k][1], highs[-k][0]
            break

    if established:
        broken = False
        for c in candles[inval_i + 1:]:
            if established == "bullish" and c["c"] < inval_level:
                broken = True
                break
            if established == "bearish" and c["c"] > inval_level:
                broken = True
                break
        if not broken:
            strong = three_swing_aligned(highs, lows, established)
            return established, ("strong" if strong else "valid")

    ma = sma(candles, MA_PERIOD)
    if ma:
        price = candles[-1]["c"]
        prev_ma = sma(candles[:-5], MA_PERIOD) if len(candles) > MA_PERIOD + 5 else None
        if prev_ma:
            if price > ma and ma > prev_ma:
                return "bullish", "weak (MA only)"
            if price < ma and ma < prev_ma:
                return "bearish", "weak (MA only)"
    return None, "no structure"


def four_hour_zone(candles, bias):
    highs, lows = find_swings(candles, PIVOT_ZONE)
    if len(highs) < 2 or len(lows) < 2:
        return None

    a = atr(candles)
    if a <= 0:
        return None

    if bias == "bullish":
        leg_lo_i, leg_lo = lows[-1]
        after = candles[leg_lo_i:]
        if not after:
            return None
        leg_hi = max(c["h"] for c in after)
        leg = leg_hi - leg_lo
        if leg < MIN_LEG_ATR * a:
            return None
        z_top = leg_hi - ZONE_LOW   * leg
        z_bot = leg_hi - ZONE_HIGH  * leg
        z_prime = leg_hi - ZONE_PRIME * leg
        sl  = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
        prior = [p for _, p in highs[:-1] + lows[:-1]]
    else:
        leg_hi_i, leg_hi = highs[-1]
        after = candles[leg_hi_i:]
        if not after:
            return None
        leg_lo = min(c["l"] for c in after)
        leg = leg_hi - leg_lo
        if leg < MIN_LEG_ATR * a:
            return None
        z_bot = leg_lo + ZONE_LOW   * leg
        z_top = leg_lo + ZONE_HIGH  * leg
        z_prime = leg_lo + ZONE_PRIME * leg
        sl  = leg_hi + SL_BUFFER * leg
        tp1 = leg_lo - TP1_EXT * leg
        tp2 = leg_lo - TP2_EXT * leg
        prior = [p for _, p in highs[:-1] + lows[:-1]]

    confluence = any(z_bot <= p <= z_top for p in prior)
    return {"leg": leg, "leg_hi": leg_hi, "leg_lo": leg_lo,
            "z_bot": z_bot, "z_top": z_top, "z_prime": z_prime,
            "sl": sl, "tp1": tp1, "tp2": tp2,
            "confluence": confluence, "atr": a}


def one_hour_trigger(symbol, candles, zone, bias):
    z_bot, z_top = zone["z_bot"], zone["z_top"]

    recent = candles[-ZONE_LOOKBACK:]
    touched = any(c["l"] <= z_top and c["h"] >= z_bot for c in recent)
    if not touched:
        return None, None

    highs, lows = find_swings(candles, PIVOT_ENTRY)
    last = candles[-2]
    prev = candles[-3]

    if bias == "bullish" and highs:
        sh_i, sh = highs[-1]
        if last["c"] > sh and sh_i >= len(candles) - ZONE_LOOKBACK:
            return f"1H broke its last swing high {fmt(symbol, sh)}", "structure"
    if bias == "bearish" and lows:
        sl_i, sl_v = lows[-1]
        if last["c"] < sl_v and sl_i >= len(candles) - ZONE_LOOKBACK:
            return f"1H broke its last swing low {fmt(symbol, sl_v)}", "structure"

    in_zone = last["l"] <= z_top and last["h"] >= z_bot
    if in_zone:
        body = abs(last["c"] - last["o"])
        rng  = last["h"] - last["l"]
        if rng > 0:
            upper = last["h"] - max(last["c"], last["o"])
            lower = min(last["c"], last["o"]) - last["l"]
            if bias == "bullish":
                if (last["c"] > last["o"] and prev["c"] < prev["o"]
                        and last["c"] > prev["o"] and last["o"] < prev["c"]):
                    return "bullish engulfing in the zone", "candle"
                if lower > 2 * body and lower > 0.5 * rng:
                    return "bullish pin bar (rejection wick) in the zone", "candle"
            else:
                if (last["c"] < last["o"] and prev["c"] > prev["o"]
                        and last["c"] < prev["o"] and last["o"] > prev["c"]):
                    return "bearish engulfing in the zone", "candle"
                if upper > 2 * body and upper > 0.5 * rng:
                    return "bearish pin bar (rejection wick) in the zone", "candle"
    return None, None


def entry_message(symbol, bias, zone, trigger, quality, price, bias_q="valid", h4_q="valid"):
    side, emoji = ("BUY", "🟢") if bias == "bullish" else ("SELL", "🔴")
    sl, tp1, tp2 = zone["sl"], zone["tp1"], zone["tp2"]
    risk = abs(price - sl)
    rr1 = abs(tp1 - price) / risk if risk else 0
    rr2 = abs(tp2 - price) / risk if risk else 0
    lots, risk_usd = lot_size(symbol, risk, price)
    losses_to_cap = int(DAILY_LOSS_CAP / RISK_PERCENT) if RISK_PERCENT else 0

    deep = (price <= zone["z_prime"]) if bias == "bullish" else (price >= zone["z_prime"])
    grade = []
    grade.append("deep zone (0.5-0.618) ✅" if deep else "shallow zone (0.382-0.5) ⚠️")
    grade.append("4H structure confluence ✅" if zone["confluence"] else "no prior-level confluence ⚠️")
    grade.append("1H structure break ✅" if quality == "structure" else "candle confirmation only ⚠️")
    grade.append(f"daily trend: {bias_q}" + (" ✅" if bias_q in ("strong", "valid") else " ⚠️"))
    grade.append(f"4H trend: {h4_q}" + (" ✅" if h4_q in ("strong", "valid") else " ⚠️"))

    return (
        f"{emoji} *{side} — {symbol}*\n"
        f"_Daily {bias} → 4H zone → 1H evidence_\n\n"
        f"Entry (market): `{fmt(symbol, price)}`\n"
        f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, risk)})\n"
        f"TP1 (-0.382): `{fmt(symbol, tp1)}`  — RR {rr1:.2f}:1\n"
        f"TP2 (-0.618): `{fmt(symbol, tp2)}`  — RR {rr2:.2f}:1\n\n"
        + (f"📐 *Lot size: {lots:.2f}*  (risking {RISK_PERCENT}% = ${risk_usd:,.0f})\n"
           if lots > 0 else
           f"📐 *Lot size: size manually* (risk ${risk_usd:,.0f} over {dist_label(symbol, risk)})\n") +
        f"{losses_to_cap} straight losses to reach the {DAILY_LOSS_CAP}% daily cap\n\n"
        f"4H golden zone: `{fmt(symbol, zone['z_bot'])}` – `{fmt(symbol, zone['z_top'])}`\n"
        f"Trigger: {trigger}\n"
        f"Setup grade:\n• " + "\n• ".join(grade) + "\n\n"
        f"_Check the chart before entering. Not financial advice._"
    )


def armed_message(symbol, bias, zone, price, bias_q="valid"):
    side = "LONG" if bias == "bullish" else "SHORT"
    return (
        f"⚡ *ZONE ARMED — {symbol}* ({side} setup building)\n\n"
        f"Daily bias: *{bias}* ({bias_q}) · 4H impulse leg "
        f"`{fmt(symbol, zone['leg_lo'])}` → `{fmt(symbol, zone['leg_hi'])}`\n"
        f"🎯 Golden zone: `{fmt(symbol, zone['z_bot'])}` – `{fmt(symbol, zone['z_top'])}`\n"
        f"Price now: `{fmt(symbol, price)}`\n"
        f"Confluence: {'prior 4H level in zone ✅' if zone['confluence'] else 'none ⚠️'}\n\n"
        f"Planned SL `{fmt(symbol, zone['sl'])}` | TP1 `{fmt(symbol, zone['tp1'])}` | "
        f"TP2 `{fmt(symbol, zone['tp2'])}`\n\n"
        f"_Now waiting for 1H evidence (swing break or confirmation candle)._"
    )


def analyze(symbol, state):
    global USDJPY_RATE
    d = get_candles(symbol, TF_BIAS, N_BIAS)
    time.sleep(8)
    if not d or len(d) < 30:
        return f"{symbol}: daily data unavailable", None

    if symbol == "USD/JPY":
        USDJPY_RATE = d[-1]["c"]

    bias, bias_q = daily_bias(d)
    if bias is None:
        return f"{symbol}: daily {bias_q} — standing aside", None

    h4 = get_candles(symbol, TF_ZONE, N_ZONE)
    time.sleep(8)
    if not h4 or len(h4) < 40:
        return f"{symbol}: 4H data unavailable", None

    h4_bias, h4_q = daily_bias(h4, PIVOT_ZONE)
    if h4_bias != bias:
        return f"{symbol}: daily {bias} ({bias_q}), 4H not aligned — no trade", None

    zone = four_hour_zone(h4, bias)
    if zone is None:
        return f"{symbol}: daily {bias} ({bias_q}), no significant 4H leg yet", None

    h1 = get_candles(symbol, TF_ENTRY, N_ENTRY)
    time.sleep(8)
    if not h1 or len(h1) < 40:
        return f"{symbol}: 1H data unavailable", None

    price = h1[-1]["c"]
    in_zone_now  = zone["z_bot"] <= price <= zone["z_top"]
    prev_in_zone = zone["z_bot"] <= h1[-2]["c"] <= zone["z_top"]
    fresh = candle_age_minutes(h1) <= ENTRY_MAX_AGE

    trigger, quality = one_hour_trigger(symbol, h1, zone, bias)

    if trigger and fresh:
        width = max(zone["z_top"] - zone["z_bot"], 1e-9)
        pad = ZONE_TOL * width
        if not (zone["z_bot"] - pad <= price <= zone["z_top"] + pad):
            return (f"{symbol}: daily {bias} ({bias_q}) | trigger fired but price "
                    f"{fmt(symbol, price)} left the zone — skipped"), None

        risk = abs(price - zone["sl"])
        rr1 = (abs(zone["tp1"] - price) / risk) if risk > 0 else 0
        if rr1 < MIN_RR:
            return (f"{symbol}: daily {bias} ({bias_q}) | setup found but "
                    f"RR {rr1:.2f} < {MIN_RR} — skipped"), None

        if already_sent(state, symbol, bias, zone):
            return (f"{symbol}: daily {bias} ({bias_q}) | same setup already "
                    f"signalled — muted"), None

        send_telegram(entry_message(symbol, bias, zone, trigger, quality, price,
                                    bias_q, h4_q))
        mark_sent(state, symbol, bias, zone)
        return (f"{symbol}: daily {bias} ({bias_q}) | ENTRY SENT ({quality}, RR {rr1:.2f})",
                "long" if bias == "bullish" else "short")

    if in_zone_now and not prev_in_zone and not already_sent(state, symbol, "armed", zone):
        send_telegram(armed_message(symbol, bias, zone, price, bias_q))
        mark_sent(state, symbol, "armed", zone)
        return f"{symbol}: daily {bias} ({bias_q}) | entered zone — awaiting 1H evidence", None

    if in_zone_now:
        return f"{symbol}: daily {bias} ({bias_q}) | in zone, no 1H evidence yet", None

    return (f"{symbol}: daily {bias} ({bias_q}) | zone "
            f"{fmt(symbol, zone['z_bot'])}-{fmt(symbol, zone['z_top'])}, awaiting retrace"), None


def main():
    state = load_state()
    statuses, fired = [], {}
    for s in SYMBOLS:
        try:
            line, direction = analyze(s, state)
            if direction:
                fired[s] = direction
        except Exception as e:
            line = f"{s}: error ({e})"
            print(f"[error] {s}: {e}")
        statuses.append(line)
        print(line)

    if "EUR/USD" in fired and "USD/JPY" in fired:
        if fired["EUR/USD"] != fired["USD/JPY"]:
            send_telegram(
                "⚠️ *Correlation warning*\n\n"
                "EUR/USD and USD/JPY signalled opposite directions — that is the same "
                "USD bet placed twice, so your real risk is doubled.\n\n"
                f"Take one, or halve the lot size on each to keep total risk at {RISK_PERCENT}%."
            )

    if "USD/JPY" in fired and "GBP/JPY" in fired:
        if fired["USD/JPY"] == fired["GBP/JPY"]:
            send_telegram(
                "⚠️ *Correlation warning*\n\n"
                "USD/JPY and GBP/JPY signalled the same direction — both are yen bets, "
                "so your real risk is doubled.\n\n"
                f"Take one, or halve the lot size on each."
            )

    save_state(state)
    now = datetime.now(timezone.utc)

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        send_telegram(
            "🔎 *Manual check — current read*\n\n" + "\n".join(statuses) +
            f"\n\n_{now.strftime('%a %d %b %H:%M')} UTC · Daily→4H→1H_"
        )

    if now.hour == HEARTBEAT_UTC_HOUR and now.minute < HEARTBEAT_WINDOW_MIN:
        send_telegram(
            "✅ *Daily heartbeat — bot alive*\n\n" + "\n".join(statuses) +
            f"\n\n_{now.strftime('%a %d %b %H:%M')} UTC · Daily→4H→1H · "
            f"risk {RISK_PERCENT}%/trade on ${ACCOUNT_SIZE:,.0f}_"
        )


if __name__ == "__main__":
    main()
