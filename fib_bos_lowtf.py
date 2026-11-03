"""Pure Fibonacci + Break of Structure on 5m and 15m — no higher-timeframe filters.

Rules (one timeframe only):
  1. BOS: a candle CLOSES above the last confirmed swing high (bullish) or
     below the last confirmed swing low (bearish). Swings: 3 bars each side.
  2. Draw the fib on the leg that made the break, straight away:
       bullish: 1.0 = lowest low between the broken swing high and the break,
                0.0 = highest high of that leg
  3. Take the trade when price comes back into the golden zone
     (limit order at the chosen level: 0.382, 0.5 or 0.618).
  4. Stop just beyond 1.0 (+10% of the leg), TP1 at -0.382, TP2 at -0.618,
     half closed at each; after TP1 the stop on the second half moves to
     the entry (breakeven).

Variants tested on each timeframe:
  entry level   0.382 / 0.5 / 0.618
  fib anchor    "fixed"  - fib frozen on the break candle
                "extend" - 0.0 follows new highs (lows) until the retrace fills
A waiting setup is replaced by a newer break or cancelled after 60 bars. Spread cost on every entry, stop-first when a
bar touches both, a stop hit on the fill bar counts as a loss, one trade per
pair at a time, NFP blackout. 2015-2026 OANDA data; first 60% of trades =
in-sample, last 40% = out-of-sample.
"""

import gc
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import backtest_stacks as B

B.OANDA_GRAN["5m"] = "M5"
TFS = [("5m", 5), ("15m", 15)]
ENTRIES = [0.382, 0.5, 0.618]
ANCHORS = ["fixed", "extend"]
PIVOT = 3
EXPIRE_BARS = 60
MAX_HOLD_HOURS = 72
SL_BUF, TP1, TP2 = 0.10, 0.382, 0.618
LOG = []


def out(line=""):
    print(line, flush=True)
    LOG.append(line)


def load(sym, tf):
    raw = B.fetch_oanda(sym, tf)
    if not raw:
        return None
    T = [int(c["dt"].replace(tzinfo=timezone.utc).timestamp()) for c in raw]
    O = [c["o"] for c in raw]; H = [c["h"] for c in raw]
    L = [c["l"] for c in raw]; C = [c["c"] for c in raw]
    del raw
    gc.collect()
    return T, O, H, L, C


def swings(H, L, p):
    """For each bar, the swing high/low known at that bar: (price, index)."""
    n = len(H)
    sh = [None] * n
    sl = [None] * n
    last_h = last_l = None
    for i in range(n):
        j = i - p                        # a swing at j is confirmed at bar i
        if j >= p:
            wh = H[j - p:j + p + 1]
            if H[j] == max(wh):
                last_h = (H[j], j)
            wl = L[j - p:j + p + 1]
            if L[j] == min(wl):
                last_l = (L[j], j)
        sh[i], sl[i] = last_h, last_l
    return sh, sl


def run(data, entry_f, anchor, tf_min, cost, blackout):
    T, O, H, L, C, SH, SL = data
    n = len(T)
    max_bars = MAX_HOLD_HOURS * 60 // tf_min
    trades = []
    setup = None          # dict while waiting for the retrace
    broken_h = broken_l = None
    t = PIVOT * 2 + 1
    while t < n - 1:
        # ---- 1. new break of structure at the close of bar t?
        sh, sl = SH[t], SL[t]
        new = None
        if sh and sh != broken_h and C[t] > sh[0]:
            broken_h = sh
            lo_i = min(range(sh[1], t + 1), key=lambda k: L[k])
            new = {"dir": 1, "lo": L[lo_i], "hi": max(H[lo_i:t + 1]), "born": t}
        elif sl and sl != broken_l and C[t] < sl[0]:
            broken_l = sl
            hi_i = max(range(sl[1], t + 1), key=lambda k: H[k])
            new = {"dir": -1, "hi": H[hi_i], "lo": min(L[hi_i:t + 1]), "born": t}
        if new and new["hi"] > new["lo"]:
            setup = new                   # the newest break replaces any waiting setup

        # ---- 2. waiting setup: does bar t+1 fill it?
        k = t + 1
        if setup:
            leg = setup["hi"] - setup["lo"]
            if setup["dir"] == 1:
                E = setup["hi"] - entry_f * leg
                if k - setup["born"] > EXPIRE_BARS:
                    setup = None
                elif L[k] <= E:           # filled (a stop on the same bar is a loss)
                    trades += fill(setup, E, k, T, H, L, C, cost, max_bars, blackout)
                    t = trades[-1]["exit_i"] if trades and trades[-1]["born"] == setup["born"] else k
                    setup = None
                    # breaks that happened while in the trade are not traded late
                    broken_h, broken_l = SH[t], SL[t]
                    t += 1
                    continue
                elif anchor == "extend" and H[k] > setup["hi"]:
                    setup["hi"] = H[k]
            else:
                E = setup["lo"] + entry_f * leg
                if k - setup["born"] > EXPIRE_BARS:
                    setup = None
                elif H[k] >= E:           # filled (a stop on the same bar is a loss)
                    trades += fill(setup, E, k, T, H, L, C, cost, max_bars, blackout)
                    t = trades[-1]["exit_i"] if trades and trades[-1]["born"] == setup["born"] else k
                    setup = None
                    # breaks that happened while in the trade are not traded late
                    broken_h, broken_l = SH[t], SL[t]
                    t += 1
                    continue
                elif anchor == "extend" and L[k] < setup["lo"]:
                    setup["lo"] = L[k]
        t += 1
    return trades


def fill(setup, E, k, T, H, L, C, cost, max_bars, blackout):
    """Half closes at TP1; the stop on the other half then moves to the entry
    (breakeven) and it runs to TP2. Trades still open after MAX_HOLD_HOURS are
    closed at the market price. G = same trade without the spread cost."""
    when = datetime.fromtimestamp(T[k], timezone.utc).replace(tzinfo=None)
    if blackout and B.in_blackout(when):
        return []
    d, hi, lo = setup["dir"], setup["hi"], setup["lo"]
    leg = hi - lo
    if d == 1:
        entry = E + cost
        sl, tp1, tp2 = lo - SL_BUF * leg, hi + TP1 * leg, hi + TP2 * leg
    else:
        entry = E - cost
        sl, tp1, tp2 = hi + SL_BUF * leg, lo - TP1 * leg, lo - TP2 * leg
    risk = abs(entry - sl)
    if risk <= 0:
        return []

    def R(px, e):       # R multiple of a full position closed at px, entered at e
        return d * (px - e) / abs(e - sl)

    # stop on the fill bar counts as a loss (we can't see the order inside the bar)
    if (d == 1 and L[k] <= sl) or (d == -1 and H[k] >= sl):
        return [{"dt": when, "R": -1.0, "G": -1.0, "exit_i": k, "born": setup["born"],
                 "cost_R": cost / risk}]
    hit1, stop, j = False, sl, k
    exit_px = None
    for j in range(k + 1, min(k + 1 + max_bars, len(T))):
        if d == 1:
            s_hit, t1, t2 = L[j] <= stop, H[j] >= tp1, H[j] >= tp2
        else:
            s_hit, t1, t2 = H[j] >= stop, L[j] <= tp1, L[j] <= tp2
        if s_hit:                                    # stop first if both touched
            exit_px = stop
            break
        if t2:
            if not hit1:
                hit1 = True
            exit_px = tp2
            break
        if t1 and not hit1:
            hit1 = True
            stop = entry                             # breakeven for the second half
    if exit_px is None:
        exit_px = C[j]                               # timed out: close at market
    if hit1:
        res = 0.5 * R(tp1, entry) + 0.5 * R(exit_px, entry)
        g = 0.5 * R(tp1, E) + 0.5 * R(exit_px if exit_px != entry else E, E)
    else:
        res = R(exit_px, entry)
        g = R(exit_px, E)
    return [{"dt": when, "R": round(res, 3), "G": round(g, 3), "exit_i": j, "born": setup["born"],
             "cost_R": cost / risk}]


def stats(tr):
    if not tr:
        return None
    rs = [x["R"] for x in tr]
    wins = [r for r in rs if r > 0]
    gl = abs(sum(r for r in rs if r <= 0))
    eq = peak = dd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); dd = max(dd, peak - eq)
    span = max((tr[-1]["dt"] - tr[0]["dt"]).days, 1)
    return {"n": len(rs), "avg": eq / len(rs), "tot": eq, "win": 100 * len(wins) / len(rs),
            "pf": sum(wins) / gl if gl else float("inf"), "dd": dd, "pm": len(rs) / (span / 30.0),
            "gavg": sum(x["G"] for x in tr) / len(tr), "costR": sum(x["cost_R"] for x in tr) / len(tr)}


def main():
    out(f"PURE FIB + BOS on 5m / 15m · run {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    results = defaultdict(list)            # (tf, anchor, entry) -> trades
    for tf, tf_min in TFS:
        for sym in B.SYMBOLS:
            t0 = time.time()
            d = load(sym, tf)
            if not d:
                out(f"  {sym} {tf}: no data")
                continue
            T, O, H, L, C = d
            SH, SL = swings(H, L, PIVOT)
            data = (T, O, H, L, C, SH, SL)
            for anchor in ANCHORS:
                for ef in ENTRIES:
                    tr = run(data, ef, anchor, tf_min, B.COST[sym], True)
                    for x in tr:
                        x["sym"] = sym
                    results[(tf, anchor, ef)] += tr
            out(f"  {sym} {tf}: {len(T)} candles {datetime.utcfromtimestamp(T[0]).date()} -> "
                f"{datetime.utcfromtimestamp(T[-1]).date()} ({time.time() - t0:.0f}s)")
            del d, data, T, O, H, L, C, SH, SL
            gc.collect()

    out("")
    out("  tf   fib     entry   trades  /mo   win%  IS avg  OOS avg   OOS R    PF  maxDD  "
        "no-cost avg  cost/trade")
    out("  " + "-" * 104)
    rows = []
    for (tf, anchor, ef), tr in sorted(results.items(), key=lambda kv: (kv[0][0] != "5m", kv[0][1], kv[0][2])):
        tr.sort(key=lambda x: x["dt"])
        if len(tr) < 30:
            out(f"  {tf:4s} {anchor:7s} {ef:<6}  too few trades ({len(tr)})")
            continue
        k = int(len(tr) * 0.6)
        si, so, sa = stats(tr[:k]), stats(tr[k:]), stats(tr)
        rows.append((tf, anchor, ef, si, so, sa, tr))
        out(f"  {tf:4s} {anchor:7s} {ef:<6} {sa['n']:7d} {sa['pm']:5.0f} {sa['win']:5.1f} {si['avg']:+7.3f} "
            f"{so['avg']:+8.3f} {so['tot']:+8.0f} {so['pf']:5.2f} {so['dd']:6.0f}   {sa['gavg']:+8.3f}"
            f"      {sa['costR']:.2f}R")

    out("")
    out("  OUT-OF-SAMPLE BY PAIR (avg R per trade)")
    for tf, anchor, ef, si, so, sa, tr in rows:
        oos = tr[int(len(tr) * 0.6):]
        parts = []
        for sym in B.SYMBOLS:
            s = stats([x for x in oos if x["sym"] == sym])
            parts.append(f"{sym.replace('/', '')} " + (f"{s['avg']:+.2f}({s['n']})" if s else "-"))
        out(f"  {tf:4s} {anchor:7s} {ef:<6} " + "  ".join(parts))

    good = [r for r in rows if r[3]["avg"] > 0 and r[4]["avg"] > 0 and r[4]["n"] >= 100]
    out("")
    out("  VERDICT")
    out(f"  combinations tested           : {len(rows)}")
    out(f"  positive out-of-sample        : {sum(r[4]['avg'] > 0 for r in rows)}")
    out(f"  positive before costs (all)   : {sum(r[5]['gavg'] > 0 for r in rows)}")
    out(f"  positive in BOTH halves, 100+ : {len(good)}")
    if good:
        b = max(good, key=lambda r: r[4]["avg"])
        out(f"  -> best: {b[0]} {b[1]} entry {b[2]}  IS {b[3]['avg']:+.3f}R  OOS {b[4]['avg']:+.3f}R "
            f"over {b[4]['n']} trades")
    else:
        out("  -> no version has an edge in both halves after costs.")
    finish(rows, good)


def finish(rows, good):
    with open("fib_bos_lowtf_results.txt", "w") as f:
        f.write("\n".join(LOG) + "\n")
    if not (B.TELEGRAM_BOT_TOKEN and B.TELEGRAM_CHAT_ID):
        return
    lines = [f"{r[0]} {r[1][:3]} {r[2]}: n{r[5]['n']} OOS{r[4]['avg']:+.2f} nocost{r[5]['gavg']:+.2f}" for r in rows]
    msg = ("Pure fib + BOS, 5m/15m, 2015-2026\n" + "\n".join(lines) +
           f"\n\nPositive in both halves: {len(good)} of {len(rows)}")
    try:
        B.requests.post(f"https://api.telegram.org/bot{B.TELEGRAM_BOT_TOKEN}/sendMessage",
                        json={"chat_id": B.TELEGRAM_CHAT_ID, "text": msg}, timeout=20)
    except Exception as e:
        print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
