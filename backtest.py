"""Fib Structure Bot — BACKTEST: textbook BOS continuation filter

One question only: does requiring a confirmed Break of Structure
(a candle CLOSING beyond the previous swing high/low, per the trading
sheet) improve results over v9 as it stands?

Everything else is LOCKED to the validated settings:
  4 pairs · Daily->4H->1H · pivot 3 · min_leg_atr 2.0 · MA off
  fib zone 0.382-0.618 · SL beyond 1.0 · TP1 -0.382 · TP2 -0.618
"""

import os
import time
import bisect
import requests
from datetime import datetime, timedelta

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]
COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015, "EUR/JPY": 0.020}

TF_BIAS, TF_ZONE, TF_ENTRY = "1day", "4h", "1h"
N_BIAS, N_ZONE, N_ENTRY = 1500, 5000, 5000
BARS_MAX = 240

PIVOT, MIN_LEG_ATR, PIVOT_ENTRY = 3, 2.0, 2
ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
ZONE_LOOKBACK, MIN_RR, ZONE_TOL = 12, 1.5, 0.10


def fetch(symbol, interval, size):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={size}&apikey={API_KEY}")
    r = requests.get(url, timeout=60).json()
    try:
        vals = r["values"]
        vals.reverse()
        return [{"dt": datetime.strptime(
                    v["datetime"][:19] if len(v["datetime"]) > 10
                    else v["datetime"] + " 00:00:00", "%Y-%m-%d %H:%M:%S"),
                 "o": float(v["open"]), "h": float(v["high"]),
                 "l": float(v["low"]), "c": float(v["close"])} for v in vals]
    except (KeyError, TypeError, ValueError) as e:
        print(f"  [warn] {symbol} {interval}: {r.get('message', e)}")
        return None


def swings(candles, pivot):
    hi, lo = [], []
    for i in range(pivot, len(candles) - pivot):
        w = candles[i - pivot: i + pivot + 1]
        if candles[i]["h"] == max(c["h"] for c in w):
            hi.append((i, candles[i]["h"], i + pivot))
        if candles[i]["l"] == min(c["l"] for c in w):
            lo.append((i, candles[i]["l"], i + pivot))
    return hi, lo


_KEY = {}


def visible(sw, upto):
    if not sw:
        return []
    k = id(sw)
    keys = _KEY.get(k)
    if keys is None:
        keys = [s[2] for s in sw]
        _KEY[k] = keys
    return sw[:bisect.bisect_right(keys, upto)]


def three_swing(hi, lo, d):
    if len(hi) < 3 or len(lo) < 3:
        return False
    h = [p for _, p, _ in hi[-3:]]
    l = [p for _, p, _ in lo[-3:]]
    if d == "bullish":
        return h[0] < h[1] < h[2] and l[0] < l[1] < l[2]
    return h[0] > h[1] > h[2] and l[0] > l[1] > l[2]


def bias_at(candles, hi, lo, upto):
    highs, lows = visible(hi, upto), visible(lo, upto)
    est = lvl = idx = None
    for k in range(1, min(len(highs), len(lows))):
        hp, hc = highs[-k - 1][1], highs[-k][1]
        lp, lc = lows[-k - 1][1], lows[-k][1]
        if hc > hp and lc > lp:
            est, lvl, idx = "bullish", lows[-k][1], lows[-k][0]
            break
        if hc < hp and lc < lp:
            est, lvl, idx = "bearish", highs[-k][1], highs[-k][0]
            break
    if not est:
        return None, "none"
    for c in candles[idx + 1: upto + 1]:
        if est == "bullish" and c["c"] < lvl:
            return None, "none"
        if est == "bearish" and c["c"] > lvl:
            return None, "none"
    return est, ("strong" if three_swing(highs, lows, est) else "valid")


def atr_at(candles, upto, period=14):
    lo = max(1, upto - period + 1)
    trs = [max(candles[i]["h"] - candles[i]["l"],
               abs(candles[i]["h"] - candles[i - 1]["c"]),
               abs(candles[i]["l"] - candles[i - 1]["c"]))
           for i in range(lo, upto + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def is_continuation(candles, hi, lo, upto, bias):
    """Textbook BOS: has a candle CLOSED beyond the previous swing?

    Bullish -> price closed above the prior swing high (buyers in control).
    Bearish -> price closed below the prior swing low (sellers in control).
    False means range or reversal, which the sheet says not to trade as BOS.
    """
    highs, lows = visible(hi, upto), visible(lo, upto)
    if len(highs) < 2 or len(lows) < 2:
        return False
    if bias == "bullish":
        sh_i, sh_p, _ = highs[-2]
        return any(candles[j]["c"] > sh_p for j in range(sh_i + 1, upto + 1))
    sl_i, sl_p, _ = lows[-2]
    return any(candles[j]["c"] < sl_p for j in range(sl_i + 1, upto + 1))


def zone_at(candles, hi, lo, upto, bias):
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
    else:
        hidx, leg_hi, _ = highs[-1]
        seg = candles[hidx: upto + 1]
        if not seg:
            return None
        leg_lo = min(c["l"] for c in seg)

    leg = leg_hi - leg_lo
    if leg < MIN_LEG_ATR * a:
        return None

    if bias == "bullish":
        z_top, z_bot = leg_hi - ZONE_LOW * leg, leg_hi - ZONE_HIGH * leg
        z_pr = leg_hi - ZONE_PRIME * leg
        sl, tp1, tp2 = (leg_lo - SL_BUFFER * leg,
                        leg_hi + TP1_EXT * leg, leg_hi + TP2_EXT * leg)
    else:
        z_bot, z_top = leg_lo + ZONE_LOW * leg, leg_lo + ZONE_HIGH * leg
        z_pr = leg_lo + ZONE_PRIME * leg
        sl, tp1, tp2 = (leg_hi + SL_BUFFER * leg,
                        leg_lo - TP1_EXT * leg, leg_lo - TP2_EXT * leg)

    prior = [p for _, p, _ in highs[:-1] + lows[:-1]]
    return {"z_bot": z_bot, "z_top": z_top, "z_prime": z_pr, "sl": sl,
            "tp1": tp1, "tp2": tp2,
            "confluence": any(z_bot <= p <= z_top for p in prior)}


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


def simulate(c1, i0, entry, sl, tp1, tp2, bias):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    r1, r2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    hit1 = False
    for j in range(i0, min(i0 + BARS_MAX, len(c1))):
        h, l = c1[j]["h"], c1[j]["l"]
        if bias == "bullish":
            slh, t1, t2 = l <= sl, h >= tp1, h >= tp2
        else:
            slh, t1, t2 = h >= sl, l <= tp1, l <= tp2
        if slh:
            return {"R": round(0.5 * r1, 2) if hit1 else -1.0,
                    "res": "TP1 then SL" if hit1 else "SL", "bars": j - i0}
        if t2:
            return {"R": round(0.5 * r1 + 0.5 * r2, 2), "res": "TP2",
                    "bars": j - i0}
        if t1 and not hit1:
            hit1 = True
    return {"R": round(0.5 * r1, 2) if hit1 else 0.0, "res": "expired",
            "bars": BARS_MAX}


def map_index(src, times, lag):
    out, j = [], -1
    for t in times:
        while j + 1 < len(src) and src[j + 1]["dt"] + lag <= t:
            j += 1
        out.append(j)
    return out


def run(data, continuation_only):
    trades = []
    for sym, d in data.items():
        c1, c4, cd = d["1h"], d["4h"], d["1day"]
        hi_d, lo_d = d["sw_d"]
        hi_4, lo_4 = d["sw_4"]
        hi_1, lo_1 = d["sw_1"]
        cost = COST[sym]
        open_until = -1

        for t in range(60, len(c1) - 2):
            if t <= open_until:
                continue
            di, fi = d["map_d"][t], d["map_4"][t]
            if di < 30 or fi < 40:
                continue

            bias, q = bias_at(cd, hi_d, lo_d, di)
            if bias is None:
                continue
            b4, _ = bias_at(c4, hi_4, lo_4, fi)
            if b4 != bias:
                continue

            if continuation_only and not is_continuation(c4, hi_4, lo_4, fi, bias):
                continue

            zone = zone_at(c4, hi_4, lo_4, fi, bias)
            if zone is None:
                continue
            if trigger_at(c1, hi_1, lo_1, t, zone, bias) is None:
                continue

            nxt = c1[t + 1]["o"]
            entry = nxt + cost if bias == "bullish" else nxt - cost
            width = max(zone["z_top"] - zone["z_bot"], 1e-9)
            pad = ZONE_TOL * width
            if not (zone["z_bot"] - pad <= entry <= zone["z_top"] + pad):
                continue
            risk = abs(entry - zone["sl"])
            if risk <= 0 or (abs(zone["tp1"] - entry) / risk) < MIN_RR:
                continue

            res = simulate(c1, t + 1, entry, zone["sl"], zone["tp1"],
                           zone["tp2"], bias)
            if res is None:
                continue
            trades.append({"sym": sym, "dir": bias, "grade": q,
                           "dt": c1[t + 1]["dt"], **res})
            open_until = t + 1 + res["bars"]
    return trades


def stats(trades):
    if not trades:
        return None
    rs = [t["R"] for t in trades]
    wins = [r for r in rs if r > 0]
    gl = abs(sum(r for r in rs if r <= 0))
    streak = worst = 0
    for r in rs:
        streak = streak + 1 if r <= 0 else 0
        worst = max(worst, streak)
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    span = (max(t["dt"] for t in trades) - min(t["dt"] for t in trades)).days or 1
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "totR": eq,
            "avgR": eq / len(rs), "pf": (sum(wins) / gl) if gl else float("inf"),
            "streak": worst, "maxdd": dd, "perweek": len(rs) / (span / 7.0)}


def show(tag, s):
    print(f"  {tag:22s} {s['n']:3d} trades  {s['perweek']:.1f}/wk  "
          f"win {s['win']:5.1f}%  {s['totR']:+6.1f}R  avg {s['avgR']:+.2f}R  "
          f"PF {s['pf']:5.2f}  streak {s['streak']}  "
          f"maxDD {s['maxdd']*0.5:.1f}%")


def breakdown(trades, tag):
    print(f"\n  {tag} — by symbol")
    for sym in SYMBOLS:
        s = stats([t for t in trades if t["sym"] == sym])
        if s:
            print(f"    {sym:9s} n={s['n']:3d}  win {s['win']:5.1f}%  "
                  f"totR {s['totR']:+6.1f}  avg {s['avgR']:+.2f}R")
        else:
            print(f"    {sym:9s} no trades")
    print(f"  {tag} — by direction")
    for side in ("bullish", "bearish"):
        s = stats([t for t in trades if t["dir"] == side])
        if s:
            print(f"    {side:8s} n={s['n']:3d}  win {s['win']:5.1f}%  "
                  f"totR {s['totR']:+6.1f}  avg {s['avgR']:+.2f}R")
        else:
            print(f"    {side:8s} no trades")


def main():
    print("Fetching data...")
    data = {}
    for sym in SYMBOLS:
        d = {}
        ok = True
        for iv, n in (("1day", N_BIAS), ("4h", N_ZONE), ("1h", N_ENTRY)):
            c = fetch(sym, iv, n)
            if not c:
                ok = False
                break
            d[iv] = c
            time.sleep(8)
        if not ok:
            print(f"  {sym}: incomplete, skipped")
            continue
        times = [c["dt"] for c in d["1h"]]
        d["map_d"] = map_index(d["1day"], times, timedelta(days=1))
        d["map_4"] = map_index(d["4h"], times, timedelta(hours=4))
        d["sw_d"] = swings(d["1day"], PIVOT)
        d["sw_4"] = swings(d["4h"], PIVOT)
        d["sw_1"] = swings(d["1h"], PIVOT_ENTRY)
        data[sym] = d
        print(f"  {sym}: {len(d['1h'])} x 1h "
              f"{d['1h'][0]['dt'].date()} -> {d['1h'][-1]['dt'].date()}")

    if not data:
        print("No data.")
        return

    print("\n" + "=" * 74)
    print("  TEXTBOOK BOS CONTINUATION FILTER — does it help?")
    print(f"  locked: pivot {PIVOT} · min_leg_atr {MIN_LEG_ATR} · MA off")
    print("=" * 74 + "\n")

    off = run(data, False)
    on = run(data, True)
    s_off, s_on = stats(off), stats(on)

    if not s_off:
        print("  no trades at all — check the data")
        return
    show("v9 as it stands", s_off)
    if s_on:
        show("continuation only", s_on)
    else:
        print("  continuation only    no trades survived the filter")

    breakdown(off, "v9")
    if s_on:
        breakdown(on, "continuation")

    print("\n" + "=" * 74)
    print("  VERDICT")
    print("=" * 74)
    if not s_on or s_on["n"] < 15:
        n = s_on["n"] if s_on else 0
        print(f"  Only {n} trades survive the filter — too few to judge.")
        print("  -> Leave v9 alone.")
    else:
        removed = s_off["n"] - s_on["n"]
        diff = s_on["avgR"] - s_off["avgR"]
        print(f"  Filter removes {removed} of {s_off['n']} trades "
              f"({100.0*removed/s_off['n']:.0f}%)")
        print(f"  Per-trade quality change: {diff:+.2f}R")
        print(f"  Total change: {s_on['totR'] - s_off['totR']:+.1f}R")
        if diff > 0.10:
            print("  -> The filter IMPROVES quality. Worth adding to v9.")
        elif diff < -0.10:
            print("  -> The filter HURTS. Leave v9 alone.")
        else:
            print("  -> No meaningful difference. Leave v9 alone (simpler wins).")

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        msg = ["*Backtest — textbook BOS filter*\n"]
        msg.append(f"*v9 as it stands*\n{s_off['n']} trades · win {s_off['win']:.1f}% · "
                   f"{s_off['totR']:+.1f}R · avg {s_off['avgR']:+.2f}R · "
                   f"PF {s_off['pf']:.2f}\n")
        if s_on:
            msg.append(f"*Continuation only*\n{s_on['n']} trades · win {s_on['win']:.1f}% · "
                       f"{s_on['totR']:+.1f}R · avg {s_on['avgR']:+.2f}R · "
                       f"PF {s_on['pf']:.2f}\n")
        else:
            msg.append("*Continuation only*\nno trades survived\n")
        msg.append("_Verdict in the Actions log._")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(msg),
                      "parse_mode": "Markdown"}, timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
