"""Fib Structure Bot — BACKTEST v8
Four validated pairs + US index ETFs (SPY/QQQ/DIA/IWM) screened as candidates.
Daily->4H->1H, MA off, no session filter, swing anchoring.
Uses your existing Twelve Data key.
Fib settings fixed: zone 0.382-0.618, SL beyond 1.0, TP1 -0.382, TP2 -0.618.
"""

import os
import time
import bisect
import requests
from datetime import datetime, timedelta

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Raw index symbols (SPX, NDX, DAX) need a paid Twelve Data plan; these
# ETFs are US equities and ARE covered by the free tier.
#   SPY = S&P 500 · QQQ = Nasdaq 100 · DIA = Dow 30 · IWM = Russell 2000
CURRENT = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]
CANDIDATES = ["SPY", "QQQ", "DIA", "IWM"]
SYMBOLS = CURRENT + CANDIDATES

COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015, "EUR/JPY": 0.020,
        "SPY": 0.05, "QQQ": 0.05, "DIA": 0.08, "IWM": 0.04}

INDICES = set(CANDIDATES)

SPEEDS = {
    "Daily -> 4H -> 1H": {"bias": "1day", "zone": "4h", "entry": "1h",
                          "bias_n": 1500, "zone_n": 5000, "entry_n": 5000,
                          "bars_max": 240},
}

USE_SESSION_FILTER = False
SESSION_START_UTC = 7
SESSION_END_UTC = 21

GRID_BOS = [False]

ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
PIVOT_ENTRY = 2
ZONE_LOOKBACK = 12
MA_PERIOD = 50
BOS_MAX_AGE = 20

MIN_RR = 1.5
ZONE_TOL = 0.10

GRID_PIVOT = [2, 3, 4]
GRID_MINLEG = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]
GRID_MA = [False]

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


def build_zone(bias, leg_hi, leg_lo, highs, lows):
    leg = leg_hi - leg_lo
    if bias == "bullish":
        z_top = leg_hi - ZONE_LOW * leg
        z_bot = leg_hi - ZONE_HIGH * leg
        z_pr = leg_hi - ZONE_PRIME * leg
        sl = leg_lo - SL_BUFFER * leg
        tp1 = leg_hi + TP1_EXT * leg
        tp2 = leg_hi + TP2_EXT * leg
    else:
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
    else:
        hidx, leg_hi, _ = highs[-1]
        seg = candles[hidx: upto + 1]
        if not seg:
            return None
        leg_lo = min(c["l"] for c in seg)

    if (leg_hi - leg_lo) < min_leg_atr * a:
        return None
    return build_zone(bias, leg_hi, leg_lo, highs, lows)


def bos_zone_at(candles, hi, lo, upto, bias, min_leg_atr):
    highs, lows = visible(hi, upto), visible(lo, upto)
    if len(highs) < 2 or len(lows) < 2:
        return None
    a = atr_at(candles, upto)
    if a <= 0:
        return None

    bos_i = None
    if bias == "bullish":
        for sh_i, sh_p, _ in reversed(highs):
            for j in range(sh_i + 1, upto + 1):
                if candles[j]["c"] > sh_p:
                    bos_i = j
                    break
            if bos_i is not None:
                break
        if bos_i is None or (upto - bos_i) > BOS_MAX_AGE:
            return None
        prior_lows = [l for l in lows if l[0] < bos_i]
        if not prior_lows:
            return None
        leg_lo = prior_lows[-1][1]
        leg_hi = max(c["h"] for c in candles[bos_i: upto + 1])
    else:
        for sl_i, sl_p, _ in reversed(lows):
            for j in range(sl_i + 1, upto + 1):
                if candles[j]["c"] < sl_p:
                    bos_i = j
                    break
            if bos_i is not None:
                break
        if bos_i is None or (upto - bos_i) > BOS_MAX_AGE:
            return None
        prior_highs = [h for h in highs if h[0] < bos_i]
        if not prior_highs:
            return None
        leg_hi = prior_highs[-1][1]
        leg_lo = min(c["l"] for c in candles[bos_i: upto + 1])

    if (leg_hi - leg_lo) < min_leg_atr * a:
        return None
    return build_zone(bias, leg_hi, leg_lo, highs, lows)


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


def simulate(c1, entry_i, entry, sl, tp1, tp2, bias, bars_max=MAX_BARS_IN_TRADE):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    r1 = abs(tp1 - entry) / risk
    r2 = abs(tp2 - entry) / risk
    hit1 = False
    for j in range(entry_i, min(entry_i + bars_max, len(c1))):
        h, l = c1[j]["h"], c1[j]["l"]
        if bias == "bullish":
            sl_hit, t1_hit, t2_hit = l <= sl, h >= tp1, h >= tp2
        else:
            sl_hit, t1_hit, t2_hit = h >= sl, l <= tp1, l <= tp2
        if sl_hit:
            return {"tp1": r1 if hit1 else -1.0, "tp2": -1.0,
                    "split": (0.5 * r1) if hit1 else -1.0, "bars": j - entry_i}
        if t2_hit:
            return {"tp1": r1, "tp2": r2,
                    "split": 0.5 * r1 + 0.5 * r2, "bars": j - entry_i}
        if t1_hit and not hit1:
            hit1 = True
    return {"tp1": r1 if hit1 else 0.0, "tp2": 0.0,
            "split": 0.5 * r1 if hit1 else 0.0, "bars": bars_max}


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


def run_combo(data, pivot, min_leg, allow_ma, spec, bars_max, use_bos):
    trades = []
    for sym, d in data.items():
        c1, c4, cd = d[spec["entry"]], d[spec["zone"]], d[spec["bias"]]
        hi_d, lo_d = cached_swings(sym, spec["bias"], cd, pivot)
        hi_4, lo_4 = cached_swings(sym, spec["zone"], c4, pivot)
        hi_1, lo_1 = cached_swings(sym, spec["entry"] + "e", c1, PIVOT_ENTRY)
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

            zone = (bos_zone_at(c4, hi_4, lo_4, fi, bias, min_leg) if use_bos
                    else zone_at(c4, hi_4, lo_4, fi, bias, min_leg))
            if zone is None:
                continue

            trg = trigger_at(c1, hi_1, lo_1, t, zone, bias)
            if trg is None:
                continue

            if USE_SESSION_FILTER:
                hr = c1[t + 1]["dt"].hour
                if not (SESSION_START_UTC <= hr < SESSION_END_UTC):
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

            res = simulate(c1, t + 1, entry, zone["sl"], zone["tp1"], zone["tp2"],
                           bias, bars_max)
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
    span = (max(t["dt"] for t in trades) - min(t["dt"] for t in trades)).days or 1
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "totR": eq,
            "avgR": eq / len(rs), "pf": (gp / gl) if gl else float("inf"),
            "streak": worst, "maxdd": dd, "perweek": len(rs) / (span / 7.0)}


def lag_for(interval):
    return {"1day": timedelta(days=1), "4h": timedelta(hours=4),
            "1h": timedelta(hours=1), "15min": timedelta(minutes=15),
            "5min": timedelta(minutes=5)}[interval]


def load_symbol(sym, spec):
    d = {}
    for key, size in (("bias", spec["bias_n"]), ("zone", spec["zone_n"]),
                      ("entry", spec["entry_n"])):
        iv = spec[key]
        if iv in d:
            continue
        c = fetch(sym, iv, size)
        if not c:
            return None
        d[iv] = c
        time.sleep(8)
    times = [c["dt"] for c in d[spec["entry"]]]
    d["map_d"] = map_index(d[spec["bias"]], times, lag_for(spec["bias"]))
    d["map_4"] = map_index(d[spec["zone"]], times, lag_for(spec["zone"]))
    return d


def report(name, data, spec):
    print("\n" + "#" * 66)
    print(f"# {name}")
    print("#" * 66)

    results = []
    for bos in GRID_BOS:
        label = "explicit BOS + freshness" if bos else "swing-based (current v9)"
        print(f"\n  --- fib anchoring: {label} ---")
        for ma in GRID_MA:
            for p in GRID_PIVOT:
                for ml in GRID_MINLEG:
                    tr = run_combo(data, p, ml, ma, spec, spec["bars_max"], bos)
                    s = stats(tr)
                    if s:
                        s.update({"pivot": p, "minleg": ml, "bos": bos, "trades": tr})
                        results.append(s)
                        print(f"    pivot={p} minleg={ml:<5} "
                              f"{s['n']:4d} trades  win {s['win']:5.1f}%  "
                              f"totR {s['totR']:+7.1f}  PF {s['pf']:5.2f}  "
                              f"streak {s['streak']:2d}  {s['perweek']:.1f}/wk")
                    else:
                        print(f"    pivot={p} minleg={ml:<5} no trades")
    if not results:
        print("  no trades in any combination")
        return None, []

    pool = [r for r in results if r["n"] >= 20] or results
    best = max(pool, key=lambda r: r["totR"])
    tr = best["trades"]

    print(f"\n  BEST OVERALL: pivot={best['pivot']} min_leg_atr={best['minleg']}")
    print(f"    {best['n']} trades | win {best['win']:.1f}% | {best['totR']:+.1f}R "
          f"| avg {best['avgR']:+.2f}R | PF {best['pf']:.2f}")
    print(f"    longest losing streak {best['streak']} | "
          f"max DD {best['maxdd']:.1f}R (~{best['maxdd']*0.5:.1f}% at 0.5% risk)")
    print(f"    frequency {best['perweek']:.1f} trades/week")

    print("\n  EXIT STYLE")
    for style in ("tp1", "tp2", "split"):
        s = stats(tr, style)
        print(f"    {style:6s} win {s['win']:5.1f}%  totR {s['totR']:+7.1f}  "
              f"avg {s['avgR']:+.2f}R  PF {s['pf']:.2f}")

    print("\n  ROBUSTNESS")
    same = sorted([r for r in results if r["pivot"] == best["pivot"]],
                  key=lambda r: r["minleg"])
    for r in same:
        mark = "  <-- BEST" if r is best else ""
        print(f"    minleg {r['minleg']:<5} n={r['n']:3d}  totR {r['totR']:+6.1f}  "
              f"PF {r['pf']:5.2f}{mark}")
    pos = sum(1 for r in same if r["totR"] > 0)
    print(f"    -> {pos}/{len(same)} profitable")
    print("    -> PLATEAU (edge is robust)" if pos >= len(same) - 1
          else "    -> SPIKE (possible curve-fit)")

    print("\n" + "=" * 74)
    print("  PAIR SCREENING — do the index ETFs earn a place?")
    print("=" * 74)
    rows = []
    for sym in SYMBOLS:
        rows.append((sym, stats([t for t in tr if t["sym"] == sym])))
    rows.sort(key=lambda r: (r[1]["totR"] if r[1] else -999), reverse=True)

    print(f"\n  settings: pivot {best['pivot']}, leg {best['minleg']}, MA off\n")
    print(f"  {'symbol':10s} {'status':10s} {'n':>4s} {'win%':>6s} "
          f"{'totR':>7s} {'avgR':>7s}  verdict")
    keepers = list(CURRENT)
    for sym, s in rows:
        status = "current" if sym in CURRENT else "candidate"
        if s is None:
            print(f"  {sym:10s} {status:10s} {'-':>4s} {'-':>6s} {'-':>7s} "
                  f"{'-':>7s}  no data / no trades")
            continue
        if s["n"] < 8:
            verdict = "TOO FEW TRADES - ignore"
        elif s["totR"] > 0 and s["avgR"] >= 0.30:
            verdict = "ADD" if sym not in CURRENT else "KEEP"
            if sym not in CURRENT:
                keepers.append(sym)
        elif s["totR"] > 0:
            verdict = "marginal - skip"
        else:
            verdict = "REJECT" if sym not in CURRENT else "DROP"
        print(f"  {sym:10s} {status:10s} {s['n']:4d} {s['win']:6.1f} "
              f"{s['totR']:+7.1f} {s['avgR']:+7.2f}  {verdict}")

    print(f"\n  Suggested SYMBOLS list: {keepers}")
    print("  (a candidate needs 8+ trades, positive totR and avgR >= +0.30R)")

    idx_days = {}
    for t in tr:
        if t["sym"] in CANDIDATES:
            idx_days.setdefault(t["dt"].date(), []).append(t["sym"])
    clash = {d: v for d, v in idx_days.items() if len(v) > 1}
    if idx_days:
        print("\n  ETF NOTE: US equities trade ~14:30-21:00 UTC only, so they")
        print("  produce about a third of the hourly candles a forex pair does.")
        print("  Fewer bars means fewer setups — judge them on avgR, not count.")
        print("\n  INDEX OVERLAP")
        print(f"    {len(clash)} of {len(idx_days)} index signal-days "
              f"had 2+ indices firing together")
        for d, v in list(clash.items())[:5]:
            print(f"      {d}: {', '.join(v)}")
        print("    (indices move together — simultaneous signals are ONE bet)")

    cur = stats([t for t in tr if t["sym"] in CURRENT])
    new = stats([t for t in tr if t["sym"] in keepers])
    if cur and new:
        print(f"\n  current four : {cur['n']:3d} trades  {cur['totR']:+6.1f}R  "
              f"avg {cur['avgR']:+.2f}R  {cur['perweek']:.1f}/wk")
        print(f"  suggested set: {new['n']:3d} trades  {new['totR']:+6.1f}R  "
              f"avg {new['avgR']:+.2f}R  {new['perweek']:.1f}/wk")
        if new["avgR"] < cur["avgR"] - 0.05:
            print("  -> expanding DILUTES the edge. Stay with four.")
        elif new["perweek"] > cur["perweek"] * 1.3:
            print("  -> expanding adds frequency without hurting quality.")
        else:
            print("  -> roughly neutral; no strong reason to change.")

    print("\n  CHALLENGE MATH (0.5% risk per trade)")
    if best["avgR"] > 0:
        weeks = (10.0 / (best["avgR"] * 0.5)) / best["perweek"]
        need = int(10.0 / (best["avgR"] * 0.5))
        print(f"    avg {best['avgR']:+.2f}R = {best['avgR']*0.5:+.2f}% per trade")
        print(f"    10% target needs ~{need} net trades = ~{weeks:.0f} weeks")
    print(f"    worst drawdown: {best['maxdd']*0.5:.1f}% (FundedNext kills at 10%)")
    print(f"    worst streak: {best['streak']} = {best['streak']*0.5:.1f}% in one day")
    return best, results


def main():
    summary = {}
    for name, spec in SPEEDS.items():
        print(f"\nFetching data for {name}...")
        data = {}
        for sym in SYMBOLS:
            d = load_symbol(sym, spec)
            if d is None:
                print(f"  {sym}: incomplete, skipped")
                continue
            data[sym] = d
            e = d[spec["entry"]]
            print(f"  {sym}: {len(e)} x {spec['entry']} "
                  f"{e[0]['dt'].date()} -> {e[-1]['dt'].date()}")
        if not data:
            print("  no data")
            continue
        _SWCACHE.clear()
        _KEYCACHE.clear()
        best, results = report(name, data, spec)
        if best:
            summary[name] = (best, results)

    if not summary:
        print("\nNothing to report.")
        return

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        lines = ["*Backtest — index ETF screening*\n"]
        for name, (best, results) in summary.items():
            tr = best["trades"]
            adds = []
            for sym in CANDIDATES:
                s = stats([t for t in tr if t["sym"] == sym])
                if s and s["n"] >= 8 and s["totR"] > 0 and s["avgR"] >= 0.30:
                    adds.append(f"{sym} ({s['totR']:+.1f}R, avg {s['avgR']:+.2f})")
            cur = stats([t for t in tr if t["sym"] in CURRENT])
            if cur:
                lines.append(f"Current four: {cur['n']} trades · {cur['totR']:+.1f}R · "
                             f"avg {cur['avgR']:+.2f}R\n")
            lines.append("*ETFs that passed:*\n" +
                         ("\n".join(adds) if adds else "none") + "\n")
        lines.append("_Full table in the Actions log._")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": "\n".join(lines),
                      "parse_mode": "Markdown"}, timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
