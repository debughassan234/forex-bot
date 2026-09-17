"""Fib BOS — ZONE / STOP / TARGET SWEEP
Twelve complete geometries tested on XAU, EUR/USD, USD/JPY, EUR/JPY, XAG.
2015-2026 via OANDA, in-sample / out-of-sample split, MFE/MAE, attribution.
"""

import os
import time
import bisect
import requests
from collections import defaultdict
from datetime import datetime, timedelta

TWELVE_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
OANDA_KEY = os.environ.get("OANDA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_SOURCE = os.environ.get("DATA_SOURCE", "oanda" if OANDA_KEY else "twelve")

CURRENT = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]
CANDIDATES = ["XAG/USD"]
SYMBOLS = CURRENT + CANDIDATES

COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015,
        "EUR/JPY": 0.020, "XAG/USD": 0.020}

# zone = entry band · stop = "leg" / "0886" / "atr" · target = "ext" / "r1" / "r2"
VARIANTS = [
    ("current 0.382-0.618 | leg stop | ext",   (0.382, 0.618), "leg",  "ext"),
    ("current zone | leg stop | 1R",           (0.382, 0.618), "leg",  "r1"),
    ("current zone | leg stop | 2R",           (0.382, 0.618), "leg",  "r2"),
    ("deep 0.5-0.786 | leg stop | ext",        (0.500, 0.786), "leg",  "ext"),
    ("deep 0.5-0.786 | 0.886 stop | ext",      (0.500, 0.786), "0886", "ext"),
    ("deep 0.5-0.786 | 0.886 stop | 2R",       (0.500, 0.786), "0886", "r2"),
    ("deeper 0.618-0.786 | 0.886 stop | ext",  (0.618, 0.786), "0886", "ext"),
    ("deeper 0.618-0.786 | 0.886 stop | 2R",   (0.618, 0.786), "0886", "r2"),
    ("shallow 0.236-0.5 | leg stop | ext",     (0.236, 0.500), "leg",  "ext"),
    ("wide 0.382-0.786 | 0.886 stop | ext",    (0.382, 0.786), "0886", "ext"),
    ("current zone | ATR stop | 2R",           (0.382, 0.618), "atr",  "r2"),
    ("deep 0.5-0.786 | ATR stop | 2R",         (0.500, 0.786), "atr",  "r2"),
]

START_DATE = datetime(2015, 1, 1)
OOS_FRACTION = 0.40

USE_NEWS_BLACKOUT = True
NFP_BLACKOUT_HOURS = 4
US_DATA_WINDOW = (13, 15)
BLACKOUT_US_WINDOW = False

PIVOT, MIN_LEG_ATR, PIVOT_ENTRY = 3, 2.0, 2
ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
ZONE_LOOKBACK, MIN_RR, ZONE_TOL = 12, 1.5, 0.10
BARS_MAX = 240

OANDA_URL = "https://api-fxpractice.oanda.com/v3/instruments"
OANDA_GRAN = {"1day": "D", "4h": "H4", "1h": "H1"}


def fetch_twelve(symbol, interval, size=5000):
    url = (f"https://api.twelvedata.com/time_series?symbol={symbol}"
           f"&interval={interval}&outputsize={size}&apikey={TWELVE_KEY}")
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


def fetch_oanda(symbol, interval, start=START_DATE):
    inst = symbol if "_" in symbol else symbol.replace("/", "_")
    gran = OANDA_GRAN[interval]
    headers = {"Authorization": f"Bearer {OANDA_KEY}"}
    out, to_time, guard = [], None, 0

    while guard < 60:
        guard += 1
        params = {"granularity": gran, "count": 5000, "price": "M"}
        if to_time:
            params["to"] = to_time
        try:
            r = requests.get(f"{OANDA_URL}/{inst}/candles", headers=headers,
                             params=params, timeout=90)
            if r.status_code != 200:
                print(f"  [warn] {symbol} {interval}: HTTP {r.status_code} "
                      f"{r.text[:100]}")
                break
            batch = r.json().get("candles", [])
        except Exception as e:
            print(f"  [warn] {symbol} {interval}: {e}")
            break
        if not batch:
            break

        rows = []
        for cd in batch:
            if not cd.get("complete"):
                continue
            m = cd["mid"]
            rows.append({"dt": datetime.strptime(cd["time"][:19],
                                                 "%Y-%m-%dT%H:%M:%S"),
                         "o": float(m["o"]), "h": float(m["h"]),
                         "l": float(m["l"]), "c": float(m["c"])})
        if not rows:
            break
        out = rows + out
        if out[0]["dt"] <= start:
            break
        to_time = batch[0]["time"]
        time.sleep(0.2)

    return [c for c in out if c["dt"] >= start] or None


def fetch(symbol, interval):
    if DATA_SOURCE == "oanda":
        return fetch_oanda(symbol, interval)
    c = fetch_twelve(symbol, interval)
    time.sleep(8)
    return c


def first_friday(year, month):
    d = datetime(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def in_blackout(dt):
    if not USE_NEWS_BLACKOUT:
        return False
    ff = first_friday(dt.year, dt.month)
    if dt.date() == ff.date():
        nfp = ff.replace(hour=13, minute=30)
        if abs((dt - nfp).total_seconds()) <= NFP_BLACKOUT_HOURS * 3600:
            return True
    if BLACKOUT_US_WINDOW and dt.weekday() < 5:
        if US_DATA_WINDOW[0] <= dt.hour < US_DATA_WINDOW[1]:
            return True
    return False


def swings(candles, pivot):
    hi, lo = [], []
    for i in range(pivot, len(candles) - pivot):
        w = candles[i - pivot: i + pivot + 1]
        if candles[i]["h"] == max(x["h"] for x in w):
            hi.append((i, candles[i]["h"], i + pivot))
        if candles[i]["l"] == min(x["l"] for x in w):
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
    highs, lows = visible(hi, upto), visible(lo, upto)
    if len(highs) < 2 or len(lows) < 2:
        return False
    if bias == "bullish":
        sh_i, sh_p, _ = highs[-2]
        return any(candles[j]["c"] > sh_p for j in range(sh_i + 1, upto + 1))
    sl_i, sl_p, _ = lows[-2]
    return any(candles[j]["c"] < sl_p for j in range(sl_i + 1, upto + 1))


def zone_at(candles, hi, lo, upto, bias, zlow=ZONE_LOW, zhigh=ZONE_HIGH,
            stop_mode="leg"):
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
        z_bot, z_top = leg_hi - zhigh * leg, leg_hi - zlow * leg
        if stop_mode == "leg":
            sl = leg_lo - SL_BUFFER * leg
        elif stop_mode == "0886":
            sl = leg_hi - 0.886 * leg
        else:
            sl = z_bot - 1.5 * a
        return {"z_bot": z_bot, "z_top": z_top,
                "z_prime": leg_hi - ZONE_PRIME * leg, "sl": sl,
                "tp1": leg_hi + TP1_EXT * leg, "tp2": leg_hi + TP2_EXT * leg,
                "leg": leg}
    z_bot, z_top = leg_lo + zlow * leg, leg_lo + zhigh * leg
    if stop_mode == "leg":
        sl = leg_hi + SL_BUFFER * leg
    elif stop_mode == "0886":
        sl = leg_lo + 0.886 * leg
    else:
        sl = z_top + 1.5 * a
    return {"z_bot": z_bot, "z_top": z_top,
            "z_prime": leg_lo + ZONE_PRIME * leg, "sl": sl,
            "tp1": leg_lo - TP1_EXT * leg, "tp2": leg_lo - TP2_EXT * leg,
            "leg": leg}


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


def simulate(c1, i0, entry, sl, tp1, tp2, bias, tmode="ext"):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    if tmode == "ext":
        r1, r2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    else:
        flat = {"r1": 1.0, "r15": 1.5, "r2": 2.0}[tmode]
        r1 = r2 = flat
        if bias == "bullish":
            tp1 = tp2 = entry + flat * risk
        else:
            tp1 = tp2 = entry - flat * risk
    hit1 = False
    mfe = mae = 0.0

    for j in range(i0, min(i0 + BARS_MAX, len(c1))):
        c = c1[j]
        if bias == "bullish":
            fav, adv = (c["h"] - entry) / risk, (entry - c["l"]) / risk
            sl_hit, t1, t2 = c["l"] <= sl, c["h"] >= tp1, c["h"] >= tp2
        else:
            fav, adv = (entry - c["l"]) / risk, (c["h"] - entry) / risk
            sl_hit, t1, t2 = c["h"] >= sl, c["l"] <= tp1, c["l"] <= tp2
        mfe = max(mfe, fav)
        mae = max(mae, adv)

        if sl_hit:
            return {"R": round(0.5 * r1, 2) if hit1 else -1.0,
                    "res": "TP1 then SL" if hit1 else "SL",
                    "bars": j - i0, "mfe": round(mfe, 2), "mae": round(mae, 2)}
        if t2:
            return {"R": round(0.5 * r1 + 0.5 * r2, 2), "res": "TP2",
                    "bars": j - i0, "mfe": round(mfe, 2), "mae": round(mae, 2)}
        if t1 and not hit1:
            hit1 = True

    return {"R": round(0.5 * r1, 2) if hit1 else 0.0, "res": "expired",
            "bars": BARS_MAX, "mfe": round(mfe, 2), "mae": round(mae, 2)}


def map_index(src, times, lag):
    out, j = [], -1
    for t in times:
        while j + 1 < len(src) and src[j + 1]["dt"] + lag <= t:
            j += 1
        out.append(j)
    return out
        

def run(data, zlow, zhigh, stop_mode, tmode):
    trades = []
    for sym, d in data.items():
        c1, c4, cd = d["1h"], d["4h"], d["1day"]
        hi_d, lo_d = d["sw_d"]
        hi_4, lo_4 = d["sw_4"]
        hi_1, lo_1 = d["sw_1"]
        cost = COST[sym]
        busy = -1

        for t in range(60, len(c1) - 2):
            if t <= busy:
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
            if not is_continuation(c4, hi_4, lo_4, fi, bias):
                continue
            zone = zone_at(c4, hi_4, lo_4, fi, bias, zlow, zhigh, stop_mode)
            if zone is None:
                continue
            if trigger_at(c1, hi_1, lo_1, t, zone, bias) is None:
                continue

            when = c1[t + 1]["dt"]
            if in_blackout(when):
                continue

            entry = c1[t + 1]["o"] + (cost if bias == "bullish" else -cost)
            width = max(zone["z_top"] - zone["z_bot"], 1e-9)
            pad = ZONE_TOL * width
            if not (zone["z_bot"] - pad <= entry <= zone["z_top"] + pad):
                continue
            risk = abs(entry - zone["sl"])
            if risk <= 0:
                continue
            if tmode == "ext" and (abs(zone["tp1"] - entry) / risk) < MIN_RR:
                continue

            res = simulate(c1, t + 1, entry, zone["sl"], zone["tp1"],
                           zone["tp2"], bias, tmode)
            if res is None:
                continue
            trades.append({"sym": sym, "dir": bias, "grade": q, "dt": when,
                           **res})
            busy = t + 1 + res["bars"]
    return sorted(trades, key=lambda x: x["dt"])


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
            "streak": worst, "maxdd": dd, "permonth": len(rs) / (span / 30.0)}


def target_study(trades):
    if not trades:
        return
    n = len(trades)
    print("\n  TARGET STUDY - how far trades ran (MFE in R)")
    for b in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0):
        reached = sum(1 for t in trades if t["mfe"] >= b)
        print(f"    reached {b:>4.1f}R : {reached:4d} / {n} "
              f"({100.0*reached/n:5.1f}%)")
    print("\n  STOP STUDY - how far against you first (MAE in R)")
    for b in (0.25, 0.5, 0.75, 1.0):
        under = sum(1 for t in trades if t["mae"] <= b)
        print(f"    MAE under {b:>4.2f}R : {under:4d} / {n} "
              f"({100.0*under/n:5.1f}%)")
    winners = [t for t in trades if t["R"] > 0]
    if winners:
        print(f"    average MAE on winners: "
              f"{sum(t['mae'] for t in winners)/len(winners):.2f}R")


def attribution(trades):
    print("\n  R BY YEAR")
    by_year = defaultdict(list)
    for t in trades:
        by_year[t["dt"].year].append(t)
    for y in sorted(by_year):
        s = stats(by_year[y])
        print(f"    {y}  n={s['n']:4d}  win {s['win']:5.1f}%  "
              f"{s['totR']:+7.1f}R  avg {s['avgR']:+.2f}R")

    print("\n  R BY INSTRUMENT")
    for sym in SYMBOLS:
        s = stats([t for t in trades if t["sym"] == sym])
        tag = "current" if sym in CURRENT else "candidate"
        if s:
            print(f"    {sym:9s} {tag:10s} n={s['n']:4d}  win {s['win']:5.1f}%  "
                  f"{s['totR']:+7.1f}R  avg {s['avgR']:+.2f}R")
        else:
            print(f"    {sym:9s} {tag:10s} no trades")

    print("\n  R BY DIRECTION")
    for side in ("bullish", "bearish"):
        s = stats([t for t in trades if t["dir"] == side])
        if s:
            print(f"    {side:8s} n={s['n']:4d}  win {s['win']:5.1f}%  "
                  f"{s['totR']:+7.1f}R  avg {s['avgR']:+.2f}R")


def main():
    print(f"Data source: {DATA_SOURCE}")
    data = {}
    for sym in SYMBOLS:
        d, ok = {}, True
        for iv in ("1day", "4h", "1h"):
            c = fetch(sym, iv)
            if not c or len(c) < 100:
                ok = False
                break
            d[iv] = c
        if not ok:
            print(f"  {sym}: insufficient data, skipped")
            continue
        times = [c["dt"] for c in d["1h"]]
        d["map_d"] = map_index(d["1day"], times, timedelta(days=1))
        d["map_4"] = map_index(d["4h"], times, timedelta(hours=4))
        d["sw_d"] = swings(d["1day"], PIVOT)
        d["sw_4"] = swings(d["4h"], PIVOT)
        d["sw_1"] = swings(d["1h"], PIVOT_ENTRY)
        data[sym] = d
        print(f"  {sym}: {len(d['1h'])} x 1h  "
              f"{d['1h'][0]['dt'].date()} -> {d['1h'][-1]['dt'].date()}")

    if not data:
        print("No data.")
        return

    print("\n" + "=" * 98)
    print("  ZONE / STOP / TARGET SWEEP")
    print("  IS = tuning half, OOS = honest half. A variant must win BOTH.")
    print("=" * 98)
    print(f"\n  {'variant':42s} {'n':>5s} {'IS avgR':>9s} {'OOS n':>6s} "
          f"{'OOS avgR':>9s} {'OOS totR':>9s} {'win%':>6s} {'PF':>5s} {'maxDD':>7s}")
    print("  " + "-" * 96)

    rows = []
    for name, (zl, zh), smode, tmode in VARIANTS:
        tr = run(data, zl, zh, smode, tmode)
        if not tr:
            print(f"  {name:42s} {'no trades':>5s}")
            continue
        k = int(len(tr) * (1 - OOS_FRACTION))
        si, so = stats(tr[:k]), stats(tr[k:])
        if not so or not si:
            continue
        rows.append((name, si, so, tr))
        print(f"  {name:42s} {len(tr):5d} {si['avgR']:+9.3f} {so['n']:6d} "
              f"{so['avgR']:+9.3f} {so['totR']:+9.1f} {so['win']:6.1f} "
              f"{so['pf']:5.2f} {so['maxdd']:7.1f}")

    if not rows:
        print("\n  No variant produced trades.")
        return

    print("\n" + "=" * 98)
    print("  VERDICT")
    print("=" * 98)
    both = [r for r in rows if r[1]["avgR"] > 0 and r[2]["avgR"] > 0
            and r[2]["n"] >= 100]
    print(f"  variants tested               : {len(rows)}")
    print(f"  positive out-of-sample        : "
          f"{sum(1 for r in rows if r[2]['avgR'] > 0)}")
    print(f"  positive in BOTH halves, 100+ : {len(both)}")

    if not both:
        print("\n  -> NO geometry works. The zone is not the problem. The")
        print("     structure filter plus retracement entry has no edge on")
        print("     these instruments over 11 years.")
        best = max(rows, key=lambda r: r[2]["avgR"])
    else:
        best = max(both, key=lambda r: r[2]["avgR"])
        print(f"\n  -> Best consistent: {best[0]}")
        print(f"     IS {best[1]['avgR']:+.3f}R · OOS {best[2]['avgR']:+.3f}R "
              f"over {best[2]['n']} trades · PF {best[2]['pf']:.2f}")
        print("     CAUTION: 12 variants were tested. One looking good by")
        print("     chance is expected. Treat as a hypothesis, not a finding.")

    oos_tr = best[3][int(len(best[3]) * (1 - OOS_FRACTION)):]
    print(f"\n  Detail for: {best[0]}")
    attribution(oos_tr)
    target_study(oos_tr)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        s_in, s_out = best[1], best[2]
        msg = (f"*Zone sweep — 12 geometries*\n\n"
               f"Best: {best[0]}\n"
               f"OOS {s_out['n']} trades · win {s_out['win']:.1f}% · "
               f"avg {s_out['avgR']:+.3f}R · PF {s_out['pf']:.2f}\n"
               f"IS avg was {s_in['avgR']:+.3f}R\n\n"
               f"{len(both)} of {len(rows)} variants positive in both halves\n\n"
               f"_Full table in the Actions log._")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                      "parse_mode": "Markdown"}, timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
        
