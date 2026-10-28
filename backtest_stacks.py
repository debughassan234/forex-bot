"""Fib BOS — TIMEFRAME STACK TEST
Which top-down stack works, and does a strict break of structure on every
timeframe help?

Stacks (bias -> zone -> [confirm] -> trigger):
  A  Daily -> 4H -> 1H            (the live bot)
  B  4H -> 1H -> 15m
  C  Daily -> 4H -> 1H -> 15m     (1H confirms, 15m times the entry)

Each stack runs in two rule modes:
  standard  bias = HH/HL structure; 4H BOS filter; trigger = structure break
            OR engulfing / pin bar in the zone            (live-bot rules)
  strict    a candle CLOSE beyond the previous swing is required on EVERY
            timeframe: bias TF BOS + zone TF BOS + every lower timeframe
            must break structure (no candle-pattern entries)

Same fib geometry as the bot (zone 0.382-0.618, SL 1.0 + 10% leg,
TP1 -0.382 / TP2 -0.618, 50/50 split), same cost model, next-bar-open
entries, stop first on a bar that touches both, one trade per symbol at a
time, NFP blackout. 2015-2026 OANDA data. First 60% of trades = in-sample,
last 40% = out-of-sample. A stack only counts if BOTH halves are positive
with 100+ out-of-sample trades.
"""

import os
import time
import bisect
import subprocess
import requests
from collections import defaultdict
from datetime import datetime, timedelta

OANDA_KEY = os.environ.get("OANDA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOLS = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]
COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015, "EUR/JPY": 0.020}

START_DATE = datetime(2015, 1, 1)
OOS_FRACTION = 0.40
NFP_BLACKOUT_HOURS = 4

PIVOT, MIN_LEG_ATR, PIVOT_ENTRY = 3, 2.0, 2
ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
ZONE_LOOKBACK, MIN_RR, ZONE_TOL = 12, 1.5, 0.10
MAX_HOLD_HOURS = 240          # same 10-day cap on every stack
CONFIRM_LOOKBACK = 4          # stack C: 1H break must be within the last 4 1H bars

TF = {"1day": timedelta(days=1), "4h": timedelta(hours=4),
      "1h": timedelta(hours=1), "15m": timedelta(minutes=15)}
OANDA_GRAN = {"1day": "D", "4h": "H4", "1h": "H1", "15m": "M15"}
OANDA_URL = "https://api-fxpractice.oanda.com/v3/instruments"

# name, bias TF, zone TF, confirm TF (or None), trigger TF
STACKS = [
    ("A  Daily > 4H > 1H",        "1day", "4h", None, "1h"),
    ("B  4H > 1H > 15m",          "4h",   "1h", None, "15m"),
    ("C  Daily > 4H > 1H > 15m",  "1day", "4h", "1h", "15m"),
]
MODES = ["standard", "strict"]

LOG = []


def out(line=""):
    print(line, flush=True)
    LOG.append(line)


# ------------------------------------------------------------------ data
def fetch_oanda(symbol, interval, start=START_DATE):
    inst = symbol.replace("/", "_")
    headers = {"Authorization": f"Bearer {OANDA_KEY}"}
    out_c, to_time, guard = [], None, 0
    while guard < 500:
        guard += 1
        params = {"granularity": OANDA_GRAN[interval], "count": 5000, "price": "M"}
        if to_time:
            params["to"] = to_time
        batch = None
        for attempt in range(4):
            try:
                r = requests.get(f"{OANDA_URL}/{inst}/candles", headers=headers,
                                 params=params, timeout=90)
                if r.status_code == 200:
                    batch = r.json().get("candles", [])
                    break
                print(f"  [warn] {symbol} {interval}: HTTP {r.status_code} {r.text[:100]}")
                if r.status_code in (400, 401, 403, 404):
                    break
            except Exception as e:
                print(f"  [warn] {symbol} {interval}: {e}")
            time.sleep(2 * (attempt + 1))
        if not batch:
            break
        rows = []
        for cd in batch:
            if not cd.get("complete"):
                continue
            m = cd["mid"]
            rows.append({"dt": datetime.strptime(cd["time"][:19], "%Y-%m-%dT%H:%M:%S"),
                         "o": float(m["o"]), "h": float(m["h"]),
                         "l": float(m["l"]), "c": float(m["c"])})
        if not rows:
            break
        out_c = rows + out_c
        if out_c[0]["dt"] <= start:
            break
        to_time = batch[0]["time"]
        time.sleep(0.1)
    return [c for c in out_c if c["dt"] >= start] or None


def first_friday(year, month):
    d = datetime(year, month, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def in_blackout(dt):
    ff = first_friday(dt.year, dt.month)
    if dt.date() == ff.date():
        nfp = ff.replace(hour=13, minute=30)
        if abs((dt - nfp).total_seconds()) <= NFP_BLACKOUT_HOURS * 3600:
            return True
    return False


# ------------------------------------------------------------ structure
def swings(candles, pivot):
    """(index, price, index at which the swing becomes KNOWN)."""
    hi, lo = [], []
    H = [c["h"] for c in candles]
    L = [c["l"] for c in candles]
    for i in range(pivot, len(candles) - pivot):
        if H[i] == max(H[i - pivot: i + pivot + 1]):
            hi.append((i, H[i], i + pivot))
        if L[i] == min(L[i - pivot: i + pivot + 1]):
            lo.append((i, L[i], i + pivot))
    return hi, lo


class Sw:
    """Swings plus a bisect key so only swings known by bar `upto` are used."""
    def __init__(self, candles, pivot):
        self.hi, self.lo = swings(candles, pivot)
        self.khi = [s[2] for s in self.hi]
        self.klo = [s[2] for s in self.lo]

    def visible(self, upto):
        return (self.hi[:bisect.bisect_right(self.khi, upto)],
                self.lo[:bisect.bisect_right(self.klo, upto)])


def three_swing(hi, lo, d):
    if len(hi) < 3 or len(lo) < 3:
        return False
    h = [p for _, p, _ in hi[-3:]]
    l = [p for _, p, _ in lo[-3:]]
    if d == "bullish":
        return h[0] < h[1] < h[2] and l[0] < l[1] < l[2]
    return h[0] > h[1] > h[2] and l[0] > l[1] > l[2]


def bias_at(candles, sw, upto):
    highs, lows = sw.visible(upto)
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
        return None
    for c in candles[idx + 1: upto + 1]:
        if est == "bullish" and c["c"] < lvl:
            return None
        if est == "bearish" and c["c"] > lvl:
            return None
    return est


def is_continuation(candles, sw, upto, bias):
    """Textbook BOS: a candle CLOSED beyond the previous swing."""
    highs, lows = sw.visible(upto)
    if len(highs) < 2 or len(lows) < 2:
        return False
    if bias == "bullish":
        si, sp, _ = highs[-2]
        return any(candles[j]["c"] > sp for j in range(si + 1, upto + 1))
    si, sp, _ = lows[-2]
    return any(candles[j]["c"] < sp for j in range(si + 1, upto + 1))


def atr_at(candles, upto, period=14):
    lo = max(1, upto - period + 1)
    trs = [max(candles[i]["h"] - candles[i]["l"],
               abs(candles[i]["h"] - candles[i - 1]["c"]),
               abs(candles[i]["l"] - candles[i - 1]["c"]))
           for i in range(lo, upto + 1)]
    return sum(trs) / len(trs) if trs else 0.0


def zone_at(candles, sw, upto, bias):
    highs, lows = sw.visible(upto)
    if len(highs) < 2 or len(lows) < 2:
        return None
    a = atr_at(candles, upto)
    if a <= 0:
        return None
    if bias == "bullish":
        li, leg_lo, _ = lows[-1]
        seg = candles[li: upto + 1]
        leg_hi = max(c["h"] for c in seg)
    else:
        hix, leg_hi, _ = highs[-1]
        seg = candles[hix: upto + 1]
        leg_lo = min(c["l"] for c in seg)
    leg = leg_hi - leg_lo
    if leg < MIN_LEG_ATR * a:
        return None
    if bias == "bullish":
        return {"z_bot": leg_hi - ZONE_HIGH * leg, "z_top": leg_hi - ZONE_LOW * leg,
                "sl": leg_lo - SL_BUFFER * leg,
                "tp1": leg_hi + TP1_EXT * leg, "tp2": leg_hi + TP2_EXT * leg}
    return {"z_bot": leg_lo + ZONE_LOW * leg, "z_top": leg_lo + ZONE_HIGH * leg,
            "sl": leg_hi + SL_BUFFER * leg,
            "tp1": leg_lo - TP1_EXT * leg, "tp2": leg_lo - TP2_EXT * leg}


def trigger_at(c, sw, t, zone, bias, strict):
    """Closed bar t touched the zone recently and now breaks structure
    (or, in standard mode, prints an engulfing / pin bar in the zone)."""
    z_bot, z_top = zone["z_bot"], zone["z_top"]
    lo_i = max(0, t - ZONE_LOOKBACK + 1)
    if not any(x["l"] <= z_top and x["h"] >= z_bot for x in c[lo_i: t + 1]):
        return False
    highs, lows = sw.visible(t)
    last, prev = c[t], c[t - 1]
    if bias == "bullish" and highs:
        si, sp, _ = highs[-1]
        if last["c"] > sp and si >= t - ZONE_LOOKBACK:
            return True
    if bias == "bearish" and lows:
        si, sp, _ = lows[-1]
        if last["c"] < sp and si >= t - ZONE_LOOKBACK:
            return True
    if strict:
        return False
    if last["l"] <= z_top and last["h"] >= z_bot:
        body = abs(last["c"] - last["o"])
        rng = last["h"] - last["l"]
        if rng > 0:
            up = last["h"] - max(last["c"], last["o"])
            dn = min(last["c"], last["o"]) - last["l"]
            if bias == "bullish":
                if (last["c"] > last["o"] and prev["c"] < prev["o"]
                        and last["c"] > prev["o"] and last["o"] < prev["c"]):
                    return True
                if dn > 2 * body and dn > 0.5 * rng:
                    return True
            else:
                if (last["c"] < last["o"] and prev["c"] > prev["o"]
                        and last["c"] < prev["o"] and last["o"] > prev["c"]):
                    return True
                if up > 2 * body and up > 0.5 * rng:
                    return True
    return False


def simulate(c, i0, entry, sl, tp1, tp2, bias, max_bars):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    r1, r2 = abs(tp1 - entry) / risk, abs(tp2 - entry) / risk
    hit1 = False
    for j in range(i0, min(i0 + max_bars, len(c))):
        x = c[j]
        if bias == "bullish":
            sl_hit, t1, t2 = x["l"] <= sl, x["h"] >= tp1, x["h"] >= tp2
        else:
            sl_hit, t1, t2 = x["h"] >= sl, x["l"] <= tp1, x["l"] <= tp2
        if sl_hit:
            return {"R": round(0.5 * r1, 2) if hit1 else -1.0, "bars": j - i0}
        if t2:
            return {"R": round(0.5 * r1 + 0.5 * r2, 2), "bars": j - i0}
        if t1:
            hit1 = True
    return {"R": round(0.5 * r1, 2) if hit1 else 0.0, "bars": max_bars}


def closed_index(src, tf, decision_times):
    """For each decision time, the last src candle fully CLOSED by then."""
    res, j = [], -1
    for t in decision_times:
        while j + 1 < len(src) and src[j + 1]["dt"] + TF[tf] <= t:
            j += 1
        res.append(j)
    return res


# ------------------------------------------------------------------ run
def run_stack(data, stack, mode):
    name, b_tf, z_tf, c_tf, t_tf = stack
    strict = mode == "strict"
    max_bars = int(timedelta(hours=MAX_HOLD_HOURS) / TF[t_tf])
    trades = []

    for sym, d in data.items():
        cb, cz, ct = d[b_tf], d[z_tf], d[t_tf]
        swb, swz = d["sw"][(b_tf, PIVOT)], d["sw"][(z_tf, PIVOT)]
        swt = d["sw"][(t_tf, PIVOT_ENTRY)]
        mb, mz = d["map"][(t_tf, b_tf)], d["map"][(t_tf, z_tf)]
        if c_tf:
            cc, swc, mc = d[c_tf], d["sw"][(c_tf, PIVOT_ENTRY)], d["map"][(t_tf, c_tf)]
        cost = COST[sym]
        busy = -1
        cache_key, cache_val = None, None
        conf_cache = {}

        for t in range(60, len(ct) - 2):
            if t <= busy:
                continue
            bi, zi = mb[t], mz[t]
            if bi < 30 or zi < 40:
                continue

            # higher-timeframe read only changes when a new bar closes
            key = (bi, zi)
            if key != cache_key:
                cache_key, cache_val = key, None
                bias = bias_at(cb, swb, bi)
                if (bias and (not strict or is_continuation(cb, swb, bi, bias))
                        and bias_at(cz, swz, zi) == bias
                        and is_continuation(cz, swz, zi, bias)):
                    zone = zone_at(cz, swz, zi, bias)
                    if zone:
                        cache_val = (bias, zone)
                conf_cache = {}
            if cache_val is None:
                continue
            bias, zone = cache_val

            if c_tf:   # stack C: 1H must have broken (or confirmed) recently
                ci = mc[t]
                if ci < 40:
                    continue
                ok = conf_cache.get(ci)
                if ok is None:
                    ok = any(trigger_at(cc, swc, h, zone, bias, strict)
                             for h in range(max(41, ci - CONFIRM_LOOKBACK + 1), ci + 1))
                    conf_cache[ci] = ok
                if not ok:
                    continue

            if not trigger_at(ct, swt, t, zone, bias, strict):
                continue

            when = ct[t + 1]["dt"]
            if in_blackout(when):
                continue
            entry = ct[t + 1]["o"] + (cost if bias == "bullish" else -cost)
            pad = ZONE_TOL * max(zone["z_top"] - zone["z_bot"], 1e-9)
            if not (zone["z_bot"] - pad <= entry <= zone["z_top"] + pad):
                continue
            risk = abs(entry - zone["sl"])
            if risk <= 0 or abs(zone["tp1"] - entry) / risk < MIN_RR:
                continue
            res = simulate(ct, t + 1, entry, zone["sl"], zone["tp1"], zone["tp2"],
                           bias, max_bars)
            if res is None:
                continue
            trades.append({"sym": sym, "dir": bias, "dt": when, "R": res["R"]})
            busy = t + 1 + res["bars"]
    return sorted(trades, key=lambda x: x["dt"])


def stats(trades):
    if not trades:
        return None
    rs = [t["R"] for t in trades]
    wins = [r for r in rs if r > 0]
    gl = abs(sum(r for r in rs if r <= 0))
    eq = peak = dd = 0.0
    for r in rs:
        eq += r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    span = max((trades[-1]["dt"] - trades[0]["dt"]).days, 1)
    return {"n": len(rs), "win": 100.0 * len(wins) / len(rs), "totR": eq,
            "avgR": eq / len(rs), "pf": (sum(wins) / gl) if gl else float("inf"),
            "maxdd": dd, "permonth": len(rs) / (span / 30.0)}


# ------------------------------------------------------------------ main
def main():
    out(f"TIMEFRAME STACK TEST  ·  run {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    data = {}
    for sym in SYMBOLS:
        d = {}
        for iv in ("1day", "4h", "1h", "15m"):
            c = fetch_oanda(sym, iv)
            if not c or len(c) < 200:
                d = None
                break
            d[iv] = c
        if not d:
            out(f"  {sym}: insufficient data, skipped")
            continue
        d["sw"] = {}
        for iv in ("1day", "4h", "1h"):
            d["sw"][(iv, PIVOT)] = Sw(d[iv], PIVOT)
        for iv in ("1h", "15m"):
            d["sw"][(iv, PIVOT_ENTRY)] = Sw(d[iv], PIVOT_ENTRY)
        d["map"] = {}
        for t_tf in ("1h", "15m"):
            decide = [c["dt"] + TF[t_tf] for c in d[t_tf]]   # decision at bar CLOSE
            for hi_tf in ("1day", "4h", "1h"):
                if TF[hi_tf] > TF[t_tf]:
                    d["map"][(t_tf, hi_tf)] = closed_index(d[hi_tf], hi_tf, decide)
        data[sym] = d
        out(f"  {sym}: {len(d['15m'])} x 15m, {len(d['1h'])} x 1h  "
            f"{d['15m'][0]['dt'].date()} -> {d['15m'][-1]['dt'].date()}")

    if not data:
        out("No data — check OANDA_API_KEY.")
        return finish([])

    out("")
    out(f"  {'stack':26s} {'mode':9s} {'n':>5s} {'IS avg':>7s} {'OOS n':>6s} "
        f"{'OOS avg':>8s} {'OOS R':>7s} {'win%':>5s} {'PF':>5s} {'DD R':>6s} {'/mo':>5s}")
    out("  " + "-" * 100)
    rows = []
    for stack in STACKS:
        for mode in MODES:
            t0 = time.time()
            tr = run_stack(data, stack, mode)
            if len(tr) < 10:
                out(f"  {stack[0]:26s} {mode:9s} {len(tr):5d}  too few trades")
                continue
            k = int(len(tr) * (1 - OOS_FRACTION))
            si, so, sa = stats(tr[:k]), stats(tr[k:]), stats(tr)
            rows.append((stack[0], mode, si, so, sa, tr))
            out(f"  {stack[0]:26s} {mode:9s} {len(tr):5d} {si['avgR']:+7.3f} {so['n']:6d} "
                f"{so['avgR']:+8.3f} {so['totR']:+7.1f} {so['win']:5.1f} {so['pf']:5.2f} "
                f"{so['maxdd']:6.1f} {sa['permonth']:5.1f}   ({time.time()-t0:.0f}s)")

    out("")
    out("  OUT-OF-SAMPLE BY INSTRUMENT (avg R per trade)")
    for name, mode, si, so, sa, tr in rows:
        oos = tr[int(len(tr) * (1 - OOS_FRACTION)):]
        parts = []
        for sym in SYMBOLS:
            s = stats([x for x in oos if x["sym"] == sym])
            parts.append(f"{sym.split('/')[0][:3]}{sym.split('/')[1][:3]} "
                         + (f"{s['avgR']:+.2f}({s['n']})" if s else "  -  "))
        out(f"  {name[:2]} {mode:9s} " + "  ".join(parts))

    out("")
    out("  OUT-OF-SAMPLE BY YEAR (total R)")
    for name, mode, si, so, sa, tr in rows:
        oos = tr[int(len(tr) * (1 - OOS_FRACTION)):]
        by = defaultdict(float)
        for x in oos:
            by[x["dt"].year] += x["R"]
        out(f"  {name[:2]} {mode:9s} " + "  ".join(f"{y}:{v:+.0f}" for y, v in sorted(by.items())))

    both = [r for r in rows if r[2]["avgR"] > 0 and r[3]["avgR"] > 0 and r[3]["n"] >= 100]
    out("")
    out("  VERDICT")
    out(f"  combinations tested            : {len(rows)}")
    out(f"  positive out-of-sample         : {sum(1 for r in rows if r[3]['avgR'] > 0)}")
    out(f"  positive in BOTH halves, 100+  : {len(both)}")
    if both:
        b = max(both, key=lambda r: r[3]["avgR"])
        out(f"  -> best consistent: {b[0]} ({b[1]})  IS {b[2]['avgR']:+.3f}R  "
            f"OOS {b[3]['avgR']:+.3f}R over {b[3]['n']} trades")
        out("     6 combinations were tested; treat a single winner as a")
        out("     hypothesis to forward-test, not a proven edge.")
    else:
        out("  -> none of the stacks has an edge in both halves.")
    finish(rows, both)


def finish(rows, both=()):
    text = "\n".join(LOG)
    with open("backtest_stacks_results.txt", "w") as f:
        f.write(text + "\n")
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return
    if rows:
        lines = [f"{r[0][:2]}{'strict' if r[1]=='strict' else 'std':>7s} "
                 f"n{r[3]['n']:>4d} IS{r[2]['avgR']:+.2f} OOS{r[3]['avgR']:+.2f} "
                 f"PF{r[3]['pf']:.2f}" for r in rows]
        msg = ("*Timeframe stack test 2015–2026*\n"
               "A = Daily>4H>1H · B = 4H>1H>15m · C = Daily>4H>1H>15m\n"
               "std = live rules · strict = BOS on every timeframe\n"
               "```\n" + "\n".join(lines) + "\n```\n"
               f"Positive in both halves (100+ OOS trades): *{len(both)} of {len(rows)}*\n"
               "_n and PF are out-of-sample. Full table in the repo._")
    else:
        msg = "*Timeframe stack test* — no data. Check the OANDA key."
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": msg,
                            "parse_mode": "Markdown"}, timeout=20)
    except Exception as e:
        print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
