"""
Fib Structure Bot v11 — Daily -> 4H -> 1H (OANDA data, VPS)
XAU/USD, EUR/USD, USD/JPY, EUR/JPY

Backtested settings (pivot 3 | min_leg_atr 2.0 | MA fallback OFF).

v9 added: automatic TP1 / TP2 / SL hit alerts for every signal sent.
v10 adds: textbook Break-of-Structure filter — a candle must have CLOSED
beyond the previous swing before a setup qualifies.
  Backtested: win 54.8% -> 60.0%, avgR +0.51 -> +0.64, PF 2.21 -> 2.79,
  max drawdown 1.5% -> 1.1%, worst streak 3 -> 2.

Fib settings are the trader's own and unchanged:
zone 0.382-0.618, SL beyond 1.0 + buffer, TP1 -0.382, TP2 -0.618.

v11 (VPS edition):
  - price data from OANDA (free practice API, no daily call limit) instead
    of Twelve Data. Same candle source as the 11-year backtest.
  - ACCOUNT_SIZE 5000 (FundedNext 5K).
  - built to run every minute on a VPS: one heartbeat per day,
    TP/SL tracking no longer expires trades early on frequent runs.
  - `python3 fib_structure_bot.py --report` sends a status read
    without sending or recording any signals.
"""

import os
import sys
import json
import math
import time
import requests
from datetime import datetime, timedelta, timezone

OANDA_API_KEY      = os.environ["OANDA_API_KEY"]
OANDA_ENV          = os.environ.get("OANDA_ENV", "practice")   # "practice" or "live"
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]

OANDA_HOST = ("https://api-fxtrade.oanda.com" if OANDA_ENV == "live"
              else "https://api-fxpractice.oanda.com")
OANDA_GRAN = {"1day": "D", "4h": "H4", "1h": "H1"}

# --report (or a manual GitHub run): status only, no signals sent or saved
REPORT_ONLY = ("--report" in sys.argv
               or os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch")

SYMBOLS   = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]

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

MIN_LEG_ATR    = 2.0     # backtested optimum
ZONE_LOOKBACK  = 12
ENTRY_MAX_AGE  = 15
MIN_RR         = 1.5
ZONE_TOL       = 0.10
STATE_FILE     = "bot_state.json"
COOLDOWN_HOURS = 12

# ---- TP/SL hit alerts ----
TRACK_TRADES   = True
TRACK_MAX_HOURS = 240    # ~10 days before an untouched trade is abandoned

# ------- risk / position sizing (edit these to match your account) -------
ACCOUNT_SIZE   = 5000.0   # FundedNext Stellar 5K
RISK_PERCENT   = 0.5
DAILY_LOSS_CAP = 5.0
MAX_LOTS       = 5.0

CONTRACT   = {"XAU/USD": 100.0, "EUR/USD": 100000.0,
              "USD/JPY": 100000.0, "EUR/JPY": 100000.0}
JPY_QUOTED = {"USD/JPY", "EUR/JPY"}
PIP        = {"EUR/USD": 0.0001, "USD/JPY": 0.01, "EUR/JPY": 0.01}
MA_PERIOD  = 50
USE_MA_FALLBACK = False  # backtest: MA-only bias lost money in every test

USDJPY_RATE = None

HEARTBEAT_UTC_HOUR   = 7     # 8am Lagos time; sent once per day


# ---------------------------------------------------------------- state
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


# ------------------------------------------------- TP / SL hit tracking
def track_add(state, symbol, bias, price, zone):
    """Remember a signal so its TP/SL outcome can be reported."""
    if not TRACK_TRADES:
        return
    trades = state.setdefault("open_trades", [])
    trades.append({
        "sym": symbol, "dir": bias,
        "opened": datetime.now(timezone.utc).isoformat(),
        "entry": price, "sl": zone["sl"], "tp1": zone["tp1"], "tp2": zone["tp2"],
        "hit_tp1": False,
    })
    if len(trades) > 50:
        state["open_trades"] = trades[-50:]


def track_update(state, symbol, candles):
    """Resolve open trades against new candles. Returns the ones that closed."""
    if not TRACK_TRADES or not candles:
        return []

    trades = state.get("open_trades", [])
    closed, still_open = [], []

    for tr in trades:
        if tr["sym"] != symbol:
            still_open.append(tr)
            continue
        try:
            opened = datetime.fromisoformat(tr["opened"])
        except Exception:
            continue

        risk = abs(tr["entry"] - tr["sl"]) or 1e-9
        r1 = abs(tr["tp1"] - tr["entry"]) / risk
        r2 = abs(tr["tp2"] - tr["entry"]) / risk
        done = None

        # candles are re-scanned from the open each run (the TP1 flag is
        # remembered), so frequent runs no longer age trades too fast
        for c in candles:
            try:
                ct = datetime.strptime(c["t"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                continue
            if ct <= opened:
                continue

            if tr["dir"] == "bullish":
                sl_hit = c["l"] <= tr["sl"]
                t1_hit = c["h"] >= tr["tp1"]
                t2_hit = c["h"] >= tr["tp2"]
            else:
                sl_hit = c["h"] >= tr["sl"]
                t1_hit = c["l"] <= tr["tp1"]
                t2_hit = c["l"] <= tr["tp2"]

            if sl_hit:
                done = ("TP1 then SL", round(0.5 * r1, 2)) if tr["hit_tp1"] else ("SL", -1.0)
                break
            if t2_hit:
                done = ("TP2", round(0.5 * r1 + 0.5 * r2, 2))
                break
            if t1_hit and not tr["hit_tp1"]:
                tr["hit_tp1"] = True

        age_h = (datetime.now(timezone.utc) - opened).total_seconds() / 3600.0
        if done is None and age_h > TRACK_MAX_HOURS:
            done = ("expired", round(0.5 * r1, 2) if tr["hit_tp1"] else 0.0)

        if done:
            tr["result"], tr["R"] = done
            closed.append(tr)
        else:
            still_open.append(tr)

    state["open_trades"] = still_open
    return closed


# ---------------------------------------------------------------- data
def get_candles(symbol, interval, size):
    """OANDA mid-price candles, oldest first. Like Twelve Data, the last
    candle is the one still forming, so candles[-1]["c"] is the live price
    and candles[-2] is the last closed candle. Times are candle OPEN times
    in UTC. Default OANDA alignment (same as the backtest)."""
    inst = symbol.replace("/", "_")
    params = {"granularity": OANDA_GRAN[interval], "count": size, "price": "M"}
    headers = {"Authorization": f"Bearer {OANDA_API_KEY}"}
    for attempt in range(3):
        try:
            r = requests.get(f"{OANDA_HOST}/v3/instruments/{inst}/candles",
                             headers=headers, params=params, timeout=25)
            if r.status_code == 200:
                out = []
                for cd in r.json().get("candles", []):
                    m = cd["mid"]
                    out.append({"t": cd["time"][:19].replace("T", " "),
                                "o": float(m["o"]), "h": float(m["h"]),
                                "l": float(m["l"]), "c": float(m["c"]),
                                "complete": bool(cd.get("complete"))})
                return out or None
            print(f"[warn] {symbol} {interval}: HTTP {r.status_code} {r.text[:150]}")
            if r.status_code in (400, 401, 403, 404):
                return None          # bad key or symbol: retrying won't help
        except Exception as e:
            print(f"[warn] {symbol} {interval}: {e}")
        time.sleep(2 * (attempt + 1))
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
    if os.environ.get("DRY_RUN"):
        print("---- telegram (dry run) ----\n" + msg + "\n----------------------------")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=20)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 400 and "parse" in str(body.get("description", "")).lower():
            payload.pop("parse_mode")                     # formatting glitch: send as plain text
            r = requests.post(url, json=payload, timeout=20)
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            hint = {401: "bot token is wrong", 404: "bot token is wrong",
                    400: "chat ID is wrong, or press Start in your bot's chat",
                    403: "you blocked the bot, or never pressed Start"}.get(r.status_code, "")
            print(f"[warn] telegram refused the message: HTTP {r.status_code} "
                  f"{body.get('description', '')} -> {hint}")
    except Exception as e:
        # never write the token into the log
        print("[warn] telegram send failed: "
              + str(e).replace(TELEGRAM_BOT_TOKEN, "<token>"))


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
    lots = math.floor(risk_usd / per_lot_loss * 100 + 1e-9) / 100   # always round DOWN
    return min(lots, MAX_LOTS), risk_usd


def min_lot_risk(symbol, sl_distance, price):
    """Dollars lost at the stop if the smallest lot (0.01) is used."""
    loss = 0.01 * sl_distance * CONTRACT.get(symbol, 100000.0)
    if symbol in JPY_QUOTED:
        rate = price if symbol == "USD/JPY" else USDJPY_RATE
        loss = loss / rate if rate else 0.0
    return loss


# ---------------------------------------------------------------- logic
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
        lp, lc = lows[-k - 1][1], lows[-k][1]
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

    ma = sma(candles, MA_PERIOD) if USE_MA_FALLBACK else None
    if ma:
        price = candles[-1]["c"]
        prev_ma = sma(candles[:-5], MA_PERIOD) if len(candles) > MA_PERIOD + 5 else None
        if prev_ma:
            if price > ma and ma > prev_ma:
                return "bullish", "weak (MA only)"
            if price < ma and ma < prev_ma:
                return "bearish", "weak (MA only)"
    return None, "no structure"


def is_continuation(candles, bias):
    """Textbook Break of Structure: has a candle CLOSED beyond the
    previous swing high (bullish) or swing low (bearish)?

    This is step 3 of the BOS rules — the break must be confirmed by a
    CLOSE, not just a wick. Returns False for ranges and reversals, which
    the rules say not to trade as a continuation setup.
    """
    highs, lows = find_swings(candles, PIVOT_ZONE)
    if len(highs) < 2 or len(lows) < 2:
        return False
    if bias == "bullish":
        sh_i, sh_p = highs[-2]
        return any(c["c"] > sh_p for c in candles[sh_i + 1:])
    sl_i, sl_p = lows[-2]
    return any(c["c"] < sl_p for c in candles[sl_i + 1:])


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
        z_top = leg_hi - ZONE_LOW * leg
        z_bot = leg_hi - ZONE_HIGH * leg
        z_prime = leg_hi - ZONE_PRIME * leg
        sl = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
    else:
        leg_hi_i, leg_hi = highs[-1]
        after = candles[leg_hi_i:]
        if not after:
            return None
        leg_lo = min(c["l"] for c in after)
        leg = leg_hi - leg_lo
        if leg < MIN_LEG_ATR * a:
            return None
        z_bot = leg_lo + ZONE_LOW * leg
        z_top = leg_lo + ZONE_HIGH * leg
        z_prime = leg_lo + ZONE_PRIME * leg
        sl = leg_hi + SL_BUFFER * leg
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
    if not any(c["l"] <= z_top and c["h"] >= z_bot for c in recent):
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

    if last["l"] <= z_top and last["h"] >= z_bot:
        body = abs(last["c"] - last["o"])
        rng = last["h"] - last["l"]
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


# ------------------------------------------------------------- messages
def entry_message(symbol, bias, zone, trigger, quality, price, bias_q, h4_q):
    side, emoji = ("BUY", "🟢") if bias == "bullish" else ("SELL", "🔴")
    sl, tp1, tp2 = zone["sl"], zone["tp1"], zone["tp2"]
    risk = abs(price - sl)
    rr1 = abs(tp1 - price) / risk if risk else 0
    rr2 = abs(tp2 - price) / risk if risk else 0
    lots, risk_usd = lot_size(symbol, risk, price)
    losses_to_cap = int(DAILY_LOSS_CAP / RISK_PERCENT) if RISK_PERCENT else 0

    deep = (price <= zone["z_prime"]) if bias == "bullish" else (price >= zone["z_prime"])
    grade = [
        "deep zone (0.5-0.618) ✅" if deep else "shallow zone (0.382-0.5) ⚠️",
        "4H structure confluence ✅" if zone["confluence"] else "no prior-level confluence ⚠️",
        "1H structure break ✅" if quality == "structure" else "candle confirmation only ⚠️",
        "4H break of structure confirmed ✅",
        f"daily trend: {bias_q} ✅" if bias_q in ("strong", "valid") else f"daily trend: {bias_q} ⚠️",
        f"4H trend: {h4_q} ✅" if h4_q in ("strong", "valid") else f"4H trend: {h4_q} ⚠️",
    ]

    if lots >= 0.01:
        lot_line = f"📐 *Lot size: {lots:.2f}*  (risking {RISK_PERCENT}% = ${risk_usd:,.0f})\n"
    else:
        smallest = min_lot_risk(symbol, risk, price)
        lot_line = (f"📐 *Lot size: SKIP* — stop too wide for a ${ACCOUNT_SIZE:,.0f} account. "
                    f"Even 0.01 lots would risk ${smallest:,.0f} "
                    f"(limit ${risk_usd:,.0f}).\n")

    return (
        f"{emoji} *{side} — {symbol}*\n"
        f"_Daily {bias} → 4H BOS + zone → 1H evidence_\n\n"
        f"Entry (market): `{fmt(symbol, price)}`\n"
        f"Stop Loss: `{fmt(symbol, sl)}`  ({dist_label(symbol, risk)})\n"
        f"TP1 (-0.382): `{fmt(symbol, tp1)}`  — RR {rr1:.2f}:1\n"
        f"TP2 (-0.618): `{fmt(symbol, tp2)}`  — RR {rr2:.2f}:1\n\n"
        + lot_line +
        f"{losses_to_cap} straight losses to reach the {DAILY_LOSS_CAP}% daily cap\n\n"
        f"4H golden zone: `{fmt(symbol, zone['z_bot'])}` – `{fmt(symbol, zone['z_top'])}`\n"
        f"Trigger: {trigger}\n"
        f"Setup grade:\n• " + "\n• ".join(grade) + "\n\n"
        f"_Check the chart before entering. Not financial advice._"
    )


def armed_message(symbol, bias, zone, price, bias_q):
    side = "LONG" if bias == "bullish" else "SHORT"
    return (
        f"⚡ *ZONE ARMED — {symbol}* ({side} setup building)\n\n"
        f"Daily bias: *{bias}* ({bias_q}) · 4H BOS confirmed · impulse leg "
        f"`{fmt(symbol, zone['leg_lo'])}` → `{fmt(symbol, zone['leg_hi'])}`\n"
        f"🎯 Golden zone: `{fmt(symbol, zone['z_bot'])}` – `{fmt(symbol, zone['z_top'])}`\n"
        f"Price now: `{fmt(symbol, price)}`\n"
        f"Confluence: {'prior 4H level in zone ✅' if zone['confluence'] else 'none ⚠️'}\n\n"
        f"Planned SL `{fmt(symbol, zone['sl'])}` | TP1 `{fmt(symbol, zone['tp1'])}` | "
        f"TP2 `{fmt(symbol, zone['tp2'])}`\n\n"
        f"_Now waiting for 1H evidence (swing break or confirmation candle)._"
    )


def closed_message(tr):
    icon = "🎯" if (tr["R"] or 0) > 0 else "🛑"
    sym = tr["sym"]
    return (
        f"{icon} *Trade closed — {sym}*\n\n"
        f"{tr['dir'].upper()} from `{fmt(sym, tr['entry'])}`\n"
        f"Result: *{tr['result']}*  ({tr['R']:+.2f}R)\n\n"
        f"SL was `{fmt(sym, tr['sl'])}` | TP1 `{fmt(sym, tr['tp1'])}` | "
        f"TP2 `{fmt(sym, tr['tp2'])}`\n\n"
        f"_Tracked from the original signal — check your broker for the real fill._"
    )


# ------------------------------------------------------------- per symbol
def analyze(symbol, state):
    global USDJPY_RATE

    d = get_candles(symbol, TF_BIAS, N_BIAS)
    if not d or len(d) < 30:
        return f"{symbol}: daily data unavailable", None

    if symbol == "USD/JPY":
        USDJPY_RATE = d[-1]["c"]

    bias, bias_q = daily_bias(d)
    if bias is None:
        return f"{symbol}: daily {bias_q} — standing aside", None

    h4 = get_candles(symbol, TF_ZONE, N_ZONE)
    if not h4 or len(h4) < 40:
        return f"{symbol}: 4H data unavailable", None

    h4_bias, h4_q = daily_bias(h4, PIVOT_ZONE)
    if h4_bias != bias:
        return f"{symbol}: daily {bias} ({bias_q}), 4H not aligned — no trade", None

    if not is_continuation(h4, bias):
        return (f"{symbol}: daily {bias} ({bias_q}), 4H has no confirmed "
                f"break of structure — no trade"), None

    zone = four_hour_zone(h4, bias)
    if zone is None:
        return f"{symbol}: daily {bias} ({bias_q}), no significant 4H leg yet", None

    h1 = get_candles(symbol, TF_ENTRY, N_ENTRY)
    if not h1 or len(h1) < 40:
        return f"{symbol}: 1H data unavailable", None

    # weekend / quiet market: the newest candle has already closed and no new
    # one has started, so there is no live candle to read
    if h1[-1].get("complete"):
        return f"{symbol}: market quiet or closed — no live 1H candle", None

    if REPORT_ONLY:
        price = h1[-1]["c"]
        where = ("IN ZONE" if zone["z_bot"] <= price <= zone["z_top"] else "awaiting retrace")
        return (f"{symbol}: daily {bias} ({bias_q}) | zone "
                f"{fmt(symbol, zone['z_bot'])}-{fmt(symbol, zone['z_top'])}, {where}"), None

    # report any trades that hit TP or SL since the last run
    for done in track_update(state, symbol, h1):
        send_telegram(closed_message(done))
        print(f"  closed {done['sym']} {done['result']} {done['R']:+.2f}R")

    price = h1[-1]["c"]
    in_zone_now = zone["z_bot"] <= price <= zone["z_top"]
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
        track_add(state, symbol, bias, price, zone)
        return (f"{symbol}: daily {bias} ({bias_q}) | ENTRY SENT ({quality}, RR {rr1:.2f})",
                "long" if bias == "bullish" else "short")

    if in_zone_now and not prev_in_zone and not already_sent(state, symbol, "armed", zone):
        send_telegram(armed_message(symbol, bias, zone, price, bias_q))
        mark_sent(state, symbol, "armed", zone)
        return f"{symbol}: daily {bias} ({bias_q}) | entered zone — awaiting 1H evidence", None

    if in_zone_now:
        return f"{symbol}: daily {bias} ({bias_q}) | in zone, no 1H evidence yet", None

    return (f"{symbol}: daily {bias} ({bias_q}) | BOS ok, zone "
            f"{fmt(symbol, zone['z_bot'])}-{fmt(symbol, zone['z_top'])}, awaiting retrace"), None


# ------------------------------------------------------------------ main
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

    if "EUR/USD" in fired and "USD/JPY" in fired and fired["EUR/USD"] != fired["USD/JPY"]:
        send_telegram(
            "⚠️ *Correlation warning*\n\n"
            "EUR/USD and USD/JPY signalled opposite directions — that is the same "
            "USD bet placed twice, so your real risk is doubled.\n\n"
            f"Take one, or halve the lot size on each to keep total risk at {RISK_PERCENT}%."
        )

    if "USD/JPY" in fired and "EUR/JPY" in fired and fired["USD/JPY"] == fired["EUR/JPY"]:
        send_telegram(
            "⚠️ *Correlation warning*\n\n"
            "USD/JPY and EUR/JPY signalled the same direction — both are yen bets.\n\n"
            "Take one, or halve the lot size on each."
        )

    if "EUR/USD" in fired and "EUR/JPY" in fired and fired["EUR/USD"] == fired["EUR/JPY"]:
        send_telegram(
            "⚠️ *Correlation warning*\n\n"
            "EUR/USD and EUR/JPY signalled the same direction — both are euro bets.\n\n"
            "Take one, or halve the lot size on each."
        )

    now = datetime.now(timezone.utc)
    open_n = len(state.get("open_trades", []))

    hint = ""
    if all("data unavailable" in s for s in statuses):
        hint = ("\n\n⚠️ No price data at all — check the `OANDA_API_KEY` "
                "(practice token) and that `OANDA_ENV` is practice.")

    if REPORT_ONLY:
        # status read only: nothing was sent or recorded, state left untouched
        send_telegram(
            "🔎 *Manual check — current read*\n\n" + "\n".join(statuses) + hint +
            f"\n\nTracking {open_n} open trade(s)"
            f"\n_{now.strftime('%a %d %b %H:%M')} UTC · Daily→4H BOS→1H · OANDA data_"
        )
        return

    today = now.strftime("%Y-%m-%d")
    if now.hour >= HEARTBEAT_UTC_HOUR and state.get("heartbeat_day") != today:
        state["heartbeat_day"] = today
        send_telegram(
            "✅ *Daily heartbeat — bot alive*\n\n" + "\n".join(statuses) +
            f"\n\nTracking {open_n} open trade(s)"
            f"\n_{now.strftime('%a %d %b %H:%M')} UTC · risk {RISK_PERCENT}%/trade "
            f"on ${ACCOUNT_SIZE:,.0f} · OANDA data_"
        )

    save_state(state)


if __name__ == "__main__":
    main()
