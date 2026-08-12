"""
Fib Structure Bot — BACKTEST ENGINE (matches v7.1 exactly)

Replays the live strategy over real history. Tests every combination of
PIVOT_BIAS (2,3,4) x MIN_LEG_ATR (1.0-2.5) x MA fallback (on/off).

No lookahead: a swing is not "known" until pivot candles after it print.
Entry at the next candle's open with spread deducted. If a bar touches
both stop and target, the stop is assumed first.
"""

import os
import bisect
import time
import json
import requests
from datetime import datetime, timedelta

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = ["XAU/USD", "EUR/USD", "USD/JPY", "GBP/JPY"]

COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015, "GBP/JPY": 0.025}

ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
PIVOT_ENTRY = 2
ZONE_LOOKBACK = 12
MA_PERIOD = 50

# --- v7.1 gates (must match the live bot exactly) ---
MIN_RR   = 1.5
ZONE_TOL = 0.10

GRID_PIVOT = [2, 3, 4]
GRID_MINLEG = [1.0, 1.5, 2.0, 2.5]
GRID_MA = [False, True]

MAX_BARS_IN_TRADE = 240


def fetch(symbol, interval, size):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={size}&apikey={API_KEY}")
    r = requests.get(url, timeout=60).json()
    try:
        vals = r["values"]
        vals.reverse()
        out = []
        for v in vals:
            out.append({
                "dt": datetime.strptime(v["datetime"][:19] if len(v["datetime"]) > 10
                                        else v["datetime"] + " 00:00:00",
                                        "%Y-%m-%d %H:%M:%S"),
                "o": float(v["open"]), "h": float(v["high"]),
                "l": float(v["low"]), "c": float(v["close"])})
        return out
    except (KeyError, TypeError, ValueError) as e:
        print(f"  [warn] {symbol} {interval}: {r.get('message', e)}")
        return None


def swings(candles, pivot):
    highs, lows = [], []
    for i in range(pivot, len(candles) - pivot):
        w = candles[i - pivot: i + pivot + 1]
        if candles[i]["h"] == max(c["h"] for c in w):
            highs.append((i, candles[i]["h"], i + pivot))
        if candles[i]["l"] == min(c["l"] for c in w):
            lows.append((i, candles[i]["l"], i + pivot))
    return highs, lows


_KEYCACHE = {}


def _fast(sw, upto):
    kid = id(sw)
    keys = _KEYCACHE.get(kid)
    if keys is None:
        keys = [s[2] for s in sw]
        _KEYCACHE[kid] = keys
    return bisect.bisect_right(keys, upto)


def visible(sw, upto):
    if not sw:
        return []
    return sw[:_fast(sw, upto)]


def sma_at(candles, upto, period):
    if upto + 1 < period:
        return None
    return sum(c["c"] for c in candles[upto + 1 - period: upto + 1]) / period


def three_swing(highs, lows, direction):
    if len(highs) < 3 or len(lows) < 3:
        return False
    h = [p for _, p, _ in highs[-3:]]
    l = [p for _, p, _ in lows[-3:]]
    if direction == "bullish":
        return h[0] < h[1] < h[2] and l[0] < l[1] < l[2]
    return h[0] > h[1] > h[2] and l[0] > l[1] > l[2]


def bias_at(candles, hi, lo, upto, allow_ma):
    highs, lows = visible(hi, upto), visible(lo, upto)
    est = lvl = idx = None
    n = min(len(highs), len(lows))
    for k in range(1, n):
        hp, hc = highs[-k - 1][1], highs[-k][1]
        lp, lc = lows[-k - 1][1], lows[-k][1]
        if hc > hp and lc > lp:
            est, lvl, idx = "bullish", lows[-k][1], lows[-k][0]
            break
        if hc < hp and lc < lp:
            est, lvl, idx = "bearish", highs[-k][1], highs[-k][0]
            break

    if est:
        broken = False
        for c in candles[idx + 1: upto + 1]:
            if est == "bullish" and c["c"] < lvl:
                broken = True
                break
            if est == "bearish" and c["c"] > lvl:
                broken = True
                break
        if not broken:
            return est, ("strong" if three_swing(highs, lows, est) else "valid")

    if allow_ma:
        ma = sma_at(candles, upto, MA_PERIOD)
        prev = sma_at(candles, upto - 5, MA_PERIOD)
        if ma and prev:
            p = candles[upto]["c"]
            if p > ma and ma > prev:
                return "bullish", "weak"
            if p < ma and ma < prev:
                return "bearish", "weak"
    return None, "none"


def atr_at(candles, upto, period=14):
    lo = max(1, upto - period + 1)
    trs = []
    for i in range(lo, upto + 1):
        p, c = candles[i - 1], candles[i]
        trs.append(max(c["h"] - c["l"], abs(c["h"] - p["c"]), abs(c["l"] - p["c"])))
    return sum(trs) / len(trs) if trs else 0.0


def zone_at(candles, hi, lo, upto, bias, min_leg_atr):
    highs, lows = visible(hi, upto), visible(lo, upto)
    if len(highs) < 2 or len(lows) < 2:
        return None
    a = atr_at(candles, upto)
    if a <= 0:
        return None

    if bias == "bullish":
        li, leg_lo, _ = lows[-1]
        seg = candles[li: upto + 1]
        if not seg:
            return None
        leg_hi = max(c["h"] for c in seg)
        leg = leg_hi - leg_lo
        if leg < min_leg_atr * a:
            return None
        z_top = leg_hi - ZONE_LOW * leg
        z_bot = leg_hi - ZONE_HIGH * leg
        z_pr = leg_hi - ZONE_PRIME * leg
        sl = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
    else:
        hidx, leg_hi, _ = highs[-1]
        seg = candles[hidx: upto + 1]
        if not seg:
            return None
        leg_lo = min(c["l"] for c in seg)
        leg = leg_hi - leg_lo
        if leg < min_leg_atr * a:
            return None
        z_bot = leg_lo + ZONE_LOW * leg
        z_top = leg_lo + ZONE_HIGH * leg
        z_pr = leg_lo + ZONE_PRIME * leg
        sl = leg_hi + SL_BUFFER * leg
        tp1 = leg_lo - TP1_EXT * leg
        tp2 = leg_lo - TP2_EXT * leg

    prior = [p for _, p, _ in highs[:-1] + lows[:-1]]
    conf = any(z_bot <= p <= z_top for p in prior)
    return {"z_bot": z_bot, "z_top": z_top, "z_prime": z_pr,
            "sl": sl, "tp1": tp1, "tp2": tp2, "confluence": conf}


def trigger_at(c1, hi1, lo1, t, zone, bias):
    z_bot, z_top = zone["z_bot"], zone["z_top"]
    lo_i = max(0, t - ZONE_LOOKBACK + 1)
    if not any(c["l"] <= z_top and c["h"] >= z_bot for c in c1[lo_i: t + 1]):
        return None

    highs, lows = visible(hi1, t), visible(lo1, t)
    last, prev = c1[t], c1[t - 1]

    if bias == "bullish" and highs:
        si, sp, _ = highs[-1]
        if last["c"] > sp and si >= t - ZONE_LOOKBACK:
            return "structure"
    if bias == "bearish" and lows:
        si, sp, _ = lows[-1]
        if last["c"] < sp and si >= t - ZONE_LOOKBACK:
            return "structure"

    if last["l"] <= z_top and last["h"] >= z_bot:
        body = abs(last["c"] - last["o"])
        rng = last["h"] - last["l"]
        if rng > 0:
            up = last["h"] - max(last["c"], last["o"])
            dn = min(last["c"], last["o"]) - last["l"]
            if bias == "bullish":
                if (last["c"] > last["o"] and prev["c"] < prev["o"]
                        and last["c"] > prev["o"] and last["o"] < prev["c"]):
                    return "candle"
                if dn > 2 * body and dn > 0.5 * rng:
                    return "candle"
            else:
                if (last["c"] < last["o"] and prev["c"] > prev["o"]
                        and last["c"] < prev["o"] and last["o"] > prev["c"]):
                    return "candle"
                if up > 2 * body and up > 0.5 * rng:
                    return "candle"
    return None


def simulate(c1, entry_i, entry, sl, tp1, tp2, bias):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    r1 = abs(tp1 - entry) / risk
    r2 = abs(tp2 - entry) / risk
    hit1 = False
    for j in range(entry_i, min(entry_i + MAX_BARS_IN_TRADE, len(c1))):
        h, l = c1[j]["h"], c1[j]["l"]
        if bias == "bullish":
            sl_hit, t1_hit, t2_hit = l <= sl, h >= tp1, h >= tp2
        else:
            sl_hit, t1_hit, t2_hit = h >= sl, l <= tp1, l <= tp2
        if sl_hit:
            return {"tp1": r1 if hit1 else -1.0,
                    "tp2": -1.0,
                    "split": (0.5 * r1 + 0.0) if hit1 else -1.0,
                    "bars": j - entry_i}
        if t2_hit:
            return {"tp1": r1, "tp2": r2,
                    "split": 0.5 * r1 + 0.5 * r2, "bars": j - entry_i}
        if t1_hit and not hit1:
            hit1 = True
    return {"tp1": r1 if hit1 else 0.0, "tp2": 0.0,
            "split": 0.5 * r1 if hit1 else 0.0, "bars": MAX_BARS_IN_TRADE}


def map_index(src, target_times, lag):
    out, j = [], -1
    for t in target_times:
        while j + 1 < len(src) and src[j + 1]["dt"] + lag <= t:
            j += 1
        out.append(j)
    return out


_SWCACHE = {}


def cached_swings(sym, tf, candles, pivot):
    k = (sym, tf, pivot)
    if k not in _SWCACHE:
        _SWCACHE[k] = swings(candles, pivot)
    return _SWCACHE[k]


def run_combo(data, pivot, min_leg, allow_ma):
    trades = []
    for sym, d in data.items():
        c1, c4, cd = d["1h"], d["4h"], d["1day"]
        hi_d, lo_d = cached_swings(sym, "d", cd, pivot)
        hi_4, lo_4 = cached_swings(sym, "4", c4, pivot)
        hi_1, lo_1 = cached_swings(sym, "1", c1, PIVOT_ENTRY)
        idx_d, idx_4 = d["map_d"], d["map_4"]
        cost = COST[sym]

        open_until = -1
        for t in range(60, len(c1) - 2):
            if t <= open_until:
                continue
            di, fi = idx_d[t], idx_4[t]
            if di < 30 or fi < 40:
                continue

            bias, q = bias_at(cd, hi_d, lo_d, di, allow_ma)
            if bias is None:
                continue
            b4, _ = bias_at(c4, hi_4, lo_4, fi, allow_ma)
            if b4 != bias:
                continue
            zone = zone_at(c4, hi_4, lo_4, fi, bias, min_leg)
            if zone is None:
                continue
            trg = trigger_at(c1, hi_1, lo_1, t, zone, bias)
            if trg is None:
                continue

            nxt = c1[t + 1]["o"]
            entry = nxt + cost if bias == "bullish" else nxt - cost

            width = max(zone["z_top"] - zone["z_bot"], 1e-9)
            pad = ZONE_TOL * width
            if not (zone["z_bot"] - pad <= entry <= zone["z_top"] + pad):
                continue

            risk = abs(entry - zone["sl"])
            if risk <= 0:
                continue
            if (abs(zone["tp1"] - entry) / risk) < MIN_RR:
                continue

            res = simulate(c1, t + 1, entry, zone["sl"], zone["tp1"], zone["tp2"], bias)
            if res is None:
                continue
            deep = (entry <= zone["z_prime"]) if bias == "bullish" else (entry >= zone["z_prime"])
            trades.append({"sym": sym, "grade": q, "trg": trg, "deep": deep,
                           "conf": zone["confluence"], "dt": c1[t + 1]["dt"], **res})
            open_until = t + 1 + res["bars"]
    return trades


def stats(trades, key="split"):
    if not trades:
        return None
    rs = [t[key] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    eq, peak, dd = 0.0, 0.0, 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gp = sum(wins)
    gl = abs(sum(losses))
    span_days = (max(t["dt"] for t in trades) - min(t["dt"] for t in trades)).days or 1
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "totR": eq,
            "avgR": eq / len(rs), "pf": (gp / gl) if gl else float("inf"),
            "streak": worst, "maxdd": dd, "perweek": len(rs) / (span_days / 7.0)}


def main():
    print("Fetching history…")
    data = {}
    for sym in SYMBOLS:
        d = {}
        for iv, size in (("1day", 1500), ("4h", 5000), ("1h", 5000)):
            c = fetch(sym, iv, size)
            if not c:
                break
            d[iv] = c
            time.sleep(8)
        if len(d) != 3:
            print(f"  {sym}: incomplete, skipped")
            continue
        times = [c["dt"] for c in d["1h"]]
        d["map_d"] = map_index(d["1day"], times, timedelta(days=1))
        d["map_4"] = map_index(d["4h"], times, timedelta(hours=4))
        data[sym] = d
        print(f"  {sym}: {len(d['1h'])} x 1H from {d['1h'][0]['dt'].date()} "
              f"to {d['1h'][-1]['dt'].date()}")

    if not data:
        print("No data. Check the API key.")
        return

    print("\nRunning grid…")
    results = []
    for ma in GRID_MA:
        for p in GRID_PIVOT:
            for ml in GRID_MINLEG:
                tr = run_combo(data, p, ml, ma)
                s = stats(tr)
                if s:
                    s.update({"pivot": p, "minleg": ml, "ma": ma, "trades": tr})
                    results.append(s)
                    print(f"  pivot={p} minleg={ml} ma={ma}: "
                          f"{s['n']:4d} trades  win {s['win']:5.1f}%  "
                          f"totR {s['totR']:+7.1f}  PF {s['pf']:.2f}  "
                          f"streak {s['streak']}  {s['perweek']:.1f}/wk")

    if not results:
        print("No trades in any combination.")
        return

    valid = [r for r in results if r["n"] >= 20]
    pool = valid or results
    best = max(pool, key=lambda r: r["totR"])

    print("\n" + "=" * 62)
    print(f"BEST: pivot={best['pivot']} min_leg_atr={best['minleg']} MA={best['ma']}")
    print(f"  trades {best['n']} | win {best['win']:.1f}% | total {best['totR']:+.1f}R")
    print(f"  avg {best['avgR']:+.2f}R | PF {best['pf']:.2f}")
    print(f"  longest losing streak: {best['streak']}")
    print(f"  max drawdown: {best['maxdd']:.1f}R  "
          f"(= {best['maxdd'] * 0.5:.1f}% at 0.5% risk)")
    print(f"  frequency: {best['perweek']:.1f} trades/week")

    tr = best["trades"]
    for label, groups in (("BY SYMBOL", "sym"), ("BY GRADE", "grade"),
                          ("BY TRIGGER", "trg")):
        print(f"\n{label}")
        keys = sorted(set(t[groups] for t in tr))
        for k in keys:
            s = stats([t for t in tr if t[groups] == k])
            print(f"  {str(k):10s} n={s['n']:4d}  win {s['win']:5.1f}%  "
                  f"totR {s['totR']:+7.1f}  avg {s['avgR']:+.2f}R")

    print("\nDEEP ZONE (0.5-0.618) vs SHALLOW")
    for k in (True, False):
        s = stats([t for t in tr if t["deep"] == k])
        if s:
            print(f"  {'deep ' if k else 'shallow'}   n={s['n']:4d}  "
                  f"win {s['win']:5.1f}%  avg {s['avgR']:+.2f}R")

    print("\nWITH 4H CONFLUENCE vs WITHOUT")
    for k in (True, False):
        s = stats([t for t in tr if t["conf"] == k])
        if s:
            print(f"  {'yes' if k else 'no ':7s}   n={s['n']:4d}  "
                  f"win {s['win']:5.1f}%  avg {s['avgR']:+.2f}R")

    print("\nEXIT STYLE COMPARISON (best combo)")
    for style in ("tp1", "tp2", "split"):
        s = stats(tr, style)
        print(f"  {style:6s} win {s['win']:5.1f}%  totR {s['totR']:+7.1f}  "
              f"avg {s['avgR']:+.2f}R  PF {s['pf']:.2f}")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        msg = (f"📊 *Backtest complete*\n\n"
               f"Best settings: pivot `{best['pivot']}`, "
               f"min\\_leg\\_atr `{best['minleg']}`, MA fallback `{best['ma']}`\n\n"
               f"Trades: {best['n']}  ({best['perweek']:.1f}/week)\n"
               f"Win rate: {best['win']:.1f}%\n"
               f"Total: {best['totR']:+.1f}R | avg {best['avgR']:+.2f}R\n"
               f"Profit factor: {best['pf']:.2f}\n"
               f"Longest losing streak: *{best['streak']}*\n"
               f"Max drawdown: {best['maxdd']:.1f}R "
               f"(≈{best['maxdd'] * 0.5:.1f}% at 0.5% risk)\n\n"
               f"_Full breakdown in the Actions log._")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown"}, timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
