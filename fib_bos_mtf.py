"""Fib + Break of Structure using BOTH 15m and 5m together.

Two ideas, each tested in a few ways, on real OANDA data 2015-2026.

A. "Analyse on 15m, confirm on 5m"
   1. 15m BOS (a 15m candle closes beyond the last 15m swing).
   2. Fib drawn on the 15m leg that broke.
   3. Price pulls back into the 15m golden zone (0.382 / 0.5 / 0.618 tested).
   4. Wait for a 5m BOS back in the trade direction (the 5m swing it breaks must
      have formed after the 15m break) while price is still inside the 15m leg.
   5. Entry, two ways:  "now"   - market order on the next 5m candle
                        "5m fib" - limit at the 0.5 of the 5m leg that confirmed
      Stop, two ways:   "15m"   - beyond the 15m 1.0 (+10% of the 15m leg)
                        "5m"    - beyond the 5m 1.0 (+10% of the 5m leg)
      Targets: 15m fib -0.382 (half) and -0.618 (rest), breakeven after TP1.
   The 15m setup is dropped if price goes beyond the 15m 1.0, a newer 15m break
   appears, or after 15 hours.

B. "Analyse on 5m, use 15m to place the trade" - two readings:
   B1 "15m trend filter": 5m BOS + 5m fib + limit in the 5m golden zone (the
      setup tested before), taken only when the last 15m break points the same way.
   B2 "15m candle entry": 5m BOS + 5m fib; once price reaches the 5m golden zone,
      wait for a 15m candle to close in the trade direction (bullish candle that
      closes above the zone level, still inside the leg), then enter at market.
      Stop beyond the 5m 1.0, targets 5m fib -0.382 / -0.618.

Same rules as the earlier tests: spread cost on every entry, stop first when a
candle touches both, one trade per pair at a time, NFP blackout, first 60% of
trades in-sample, last 40% out-of-sample. A 15m candle is only used after it has
closed (no peeking).
"""

import gc
import traceback
import time
from collections import defaultdict
from datetime import datetime, timezone

import backtest_stacks as B
from fib_bos_lowtf import load, swings, stats, out, LOG, PIVOT

B.OANDA_GRAN["5m"] = "M5"
ZONES = [0.382, 0.5, 0.618]
SL_BUF, TP1, TP2 = 0.10, 0.382, 0.618
MAX_HOLD = 72 * 12            # 72 hours of 5m bars
EXP15 = 15 * 12               # 15m setup lives 15 hours (in 5m bars)
EXP5 = 60                     # 5m setup / pending order lives 60 5m bars (5 hours)
RESULTS_FILE = "fib_bos_mtf_results.txt"


# ----------------------------------------------------------------- helpers
def bos_events(H, L, C, SH, SL):
    """ev[t] = (dir, lo, hi, swing_idx) when bar t closes beyond the last swing."""
    n = len(C)
    ev = [None] * n
    broken_h = broken_l = None
    for t in range(PIVOT * 2 + 1, n):
        sh, sl = SH[t], SL[t]
        if sh and sh != broken_h and C[t] > sh[0]:
            broken_h = sh
            lo_i = min(range(sh[1], t + 1), key=lambda k: L[k])
            hi = max(H[lo_i:t + 1])
            if hi > L[lo_i]:
                ev[t] = (1, L[lo_i], hi, sh[1])
        elif sl and sl != broken_l and C[t] < sl[0]:
            broken_l = sl
            hi_i = max(range(sl[1], t + 1), key=lambda k: H[k])
            lo = min(L[hi_i:t + 1])
            if H[hi_i] > lo:
                ev[t] = (-1, lo, H[hi_i], sl[1])
    return ev


def done_map(T5, T15):
    """For each 5m bar i: index of the newest 15m bar that has CLOSED by the close of bar i."""
    out_, j, n15 = [-1] * len(T5), -1, len(T15)
    for i, t in enumerate(T5):
        close5 = t + 300
        while j + 1 < n15 and T15[j + 1] + 900 <= close5:
            j += 1
        out_[i] = j
    return out_


def manage(d, raw, sl, tp1, tp2, k, market, D, cost, sym):
    """Trade entered on 5m bar k at price raw (limit) or at the open of k (market).
    Half off at tp1, stop to breakeven, rest at tp2; market close after 72h."""
    T, O, H, L, C = D
    when = datetime.fromtimestamp(T[k], timezone.utc).replace(tzinfo=None)
    if B.in_blackout(when):
        return None
    entry = raw + d * cost
    if not (d * (raw - sl) > 0 and d * (tp1 - entry) > 0):
        return None
    risk = abs(entry - sl)

    def R(px, e):
        return d * (px - e) / abs(e - sl)

    hit1, stop, exit_px, j = False, sl, None, k
    first = k if market else k + 1
    if not market and ((d == 1 and L[k] <= sl) or (d == -1 and H[k] >= sl)):
        return {"dt": when, "R": -1.0, "G": -1.0, "exit_i": k, "cost_R": cost / risk, "sym": sym}
    for j in range(first, min(k + 1 + MAX_HOLD, len(T))):
        if d == 1:
            s_hit, t1, t2 = L[j] <= stop, H[j] >= tp1, H[j] >= tp2
        else:
            s_hit, t1, t2 = H[j] >= stop, L[j] <= tp1, L[j] <= tp2
        if s_hit:
            exit_px = stop
            break
        if t2:
            hit1 = True
            exit_px = tp2
            break
        if t1 and not hit1:
            hit1 = True
            stop = entry
    if exit_px is None:
        exit_px = C[j]
    if hit1:
        res = 0.5 * R(tp1, entry) + 0.5 * R(exit_px, entry)
        g = 0.5 * R(tp1, raw) + 0.5 * R(raw if exit_px == entry else exit_px, raw)
    else:
        res, g = R(exit_px, entry), R(exit_px, raw)
    return {"dt": when, "R": round(res, 3), "G": round(g, 3), "exit_i": j, "cost_R": cost / risk, "sym": sym}


# ------------------------------------------------- A: 15m analysis, 5m confirm
def run_A(P, Z, entry_mode, stop_mode, cost, sym):
    D, ev5, ev15, done, n = P["D5"], P["ev5"], P["ev15"], P["done"], P["n5"]
    T, O, H, L, C = D
    trades, s, pend = [], None, None
    last15 = -1
    i = 0
    while i < n - 1:
        # newly closed 15m candles: a new 15m break replaces the setup
        while last15 < done[i]:
            last15 += 1
            e = ev15[last15]
            if e:
                s = {"d": e[0], "lo": e[1], "hi": e[2], "born": i, "armed": False}
                pend = None
        if s:
            d, lo, hi = s["d"], s["lo"], s["hi"]
            leg = hi - lo
            if i - s["born"] > EXP15 or (d == 1 and L[i] < lo) or (d == -1 and H[i] > hi):
                s, pend = None, None
            else:
                zp = hi - Z * leg if d == 1 else lo + Z * leg
                if not s["armed"] and ((d == 1 and L[i] <= zp) or (d == -1 and H[i] >= zp)):
                    s["armed"] = True
                e5 = ev5[i]
                inside = lo < C[i] < hi
                if s["armed"] and e5 and e5[0] == d and e5[3] >= s["born"] and inside:
                    lo5, hi5 = e5[1], e5[2]
                    leg5 = hi5 - lo5
                    sl15 = lo - SL_BUF * leg if d == 1 else hi + SL_BUF * leg
                    sl5 = lo5 - SL_BUF * leg5 if d == 1 else hi5 + SL_BUF * leg5
                    sl = sl15 if stop_mode == "15m" else sl5
                    tp1 = hi + TP1 * leg if d == 1 else lo - TP1 * leg
                    tp2 = hi + TP2 * leg if d == 1 else lo - TP2 * leg
                    if entry_mode == "now":
                        tr = manage(d, O[i + 1], sl, tp1, tp2, i + 1, True, D, cost, sym)
                        if tr:
                            trades.append(tr)
                            i, s, pend = tr["exit_i"] + 1, None, None
                            last15 = done[min(i, n - 1)] if i < n else last15
                            continue
                    else:
                        lim = hi5 - 0.5 * leg5 if d == 1 else lo5 + 0.5 * leg5
                        pend = {"lim": lim, "sl": sl, "tp1": tp1, "tp2": tp2, "born": i}
            if s and pend and i + 1 < n:
                k = i + 1
                if k - pend["born"] > EXP5:
                    pend = None
                elif (d == 1 and L[k] <= pend["lim"]) or (d == -1 and H[k] >= pend["lim"]):
                    tr = manage(d, pend["lim"], pend["sl"], pend["tp1"], pend["tp2"], k, False, D, cost, sym)
                    pend = None
                    if tr:
                        trades.append(tr)
                        i, s = tr["exit_i"] + 1, None
                        last15 = done[min(i, n - 1)]
                        continue
        i += 1
    return trades


# --------------------------------------------- B1: 5m setup, 15m trend filter
def run_B1(P, Z, cost, sym):
    D, ev5, trend, n = P["D5"], P["ev5"], P["trend5"], P["n5"]
    T, O, H, L, C = D
    trades, s = [], None
    i = 0
    while i < n - 1:
        e = ev5[i]
        if e and trend[i] == e[0]:
            s = {"d": e[0], "lo": e[1], "hi": e[2], "born": i}
        elif e:
            s = None                      # newest break is against the 15m trend
        if s:
            k = i + 1
            d, lo, hi = s["d"], s["lo"], s["hi"]
            leg = hi - lo
            E = hi - Z * leg if d == 1 else lo + Z * leg
            if k - s["born"] > EXP5:
                s = None
            elif (d == 1 and L[k] <= E) or (d == -1 and H[k] >= E):
                sl = lo - SL_BUF * leg if d == 1 else hi + SL_BUF * leg
                tp1 = hi + TP1 * leg if d == 1 else lo - TP1 * leg
                tp2 = hi + TP2 * leg if d == 1 else lo - TP2 * leg
                tr = manage(d, E, sl, tp1, tp2, k, False, D, cost, sym)
                s = None
                if tr:
                    trades.append(tr)
                    i = tr["exit_i"] + 1
                    continue
        i += 1
    return trades


# ------------------------------------------ B2: 5m setup, 15m candle triggers
def run_B2(P, Z, cost, sym):
    D, ev5, done, n = P["D5"], P["ev5"], P["done"], P["n5"]
    O15, C15 = P["O15"], P["C15"]
    T, O, H, L, C = D
    trades, s = [], None
    i = 0
    while i < n - 1:
        e = ev5[i]
        if e:
            s = {"d": e[0], "lo": e[1], "hi": e[2], "born": i, "armed_at": None}
        if s:
            d, lo, hi = s["d"], s["lo"], s["hi"]
            leg = hi - lo
            zp = hi - Z * leg if d == 1 else lo + Z * leg
            if i - s["born"] > EXP5 or (d == 1 and L[i] < lo) or (d == -1 and H[i] > hi):
                s = None
            else:
                if s["armed_at"] is None and ((d == 1 and L[i] <= zp) or (d == -1 and H[i] >= zp)):
                    s["armed_at"] = done[i]          # last 15m candle closed before the touch
                j15 = done[i]
                new15 = i > 0 and done[i] != done[i - 1]
                if s["armed_at"] is not None and new15 and j15 > s["armed_at"]:
                    o15, c15 = O15[j15], C15[j15]
                    ok = (c15 > o15 and zp < c15 < hi) if d == 1 else (c15 < o15 and lo < c15 < zp)
                    if ok:
                        sl = lo - SL_BUF * leg if d == 1 else hi + SL_BUF * leg
                        tp1 = hi + TP1 * leg if d == 1 else lo - TP1 * leg
                        tp2 = hi + TP2 * leg if d == 1 else lo - TP2 * leg
                        tr = manage(d, O[i + 1], sl, tp1, tp2, i + 1, True, D, cost, sym)
                        s = None
                        if tr:
                            trades.append(tr)
                            i = tr["exit_i"] + 1
                            continue
        i += 1
    return trades


# ----------------------------------------------------------------- driver
def prepare(sym):
    d5, d15 = load(sym, "5m"), load(sym, "15m")
    if not d5 or not d15:
        return None
    T5, O5, H5, L5, C5 = d5
    T15, O15, H15, L15, C15 = d15
    SH5, SL5 = swings(H5, L5, PIVOT)
    SH15, SL15 = swings(H15, L15, PIVOT)
    ev5 = bos_events(H5, L5, C5, SH5, SL5)
    ev15 = bos_events(H15, L15, C15, SH15, SL15)
    del SH5, SL5, SH15, SL15
    done = done_map(T5, T15)
    tr15, cur = [0] * len(T15), 0
    for t, e in enumerate(ev15):
        if e:
            cur = e[0]
        tr15[t] = cur
    trend5 = [tr15[j] if j >= 0 else 0 for j in done]
    gc.collect()
    return {"D5": d5, "n5": len(T5), "ev5": ev5, "ev15": ev15, "done": done,
            "trend5": trend5, "O15": O15, "C15": C15, "span": (T5[0], T5[-1]), "n15": len(T15)}


def variants():
    v = []
    for Z in ZONES:
        for em in ("now", "5m fib"):
            for sm in ("15m", "5m"):
                v.append(("A", f"zone {Z} · {em} · stop {sm}", ("A", Z, em, sm)))
    for Z in ZONES:
        v.append(("B1", f"5m entry {Z} · 15m trend", ("B1", Z)))
    for Z in ZONES:
        v.append(("B2", f"5m zone {Z} · 15m candle", ("B2", Z)))
    return v


def simulate(P, spec, cost, sym):
    if spec[0] == "A":
        return run_A(P, spec[1], spec[2], spec[3], cost, sym)
    if spec[0] == "B1":
        return run_B1(P, spec[1], cost, sym)
    return run_B2(P, spec[1], cost, sym)


def main():
    out(f"FIB + BOS, 15m + 5m TOGETHER · real OANDA data · run {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    results = defaultdict(list)
    V = variants()
    for sym in B.SYMBOLS:
        t0 = time.time()
        P = prepare(sym)
        if not P:
            out(f"  {sym}: no data")
            continue
        for grp, name, spec in V:
            try:
                results[(grp, name)] += simulate(P, spec, B.COST[sym], sym)
            except Exception:
                out(f"  ERROR {sym} {grp} {name}: " + traceback.format_exc().strip().splitlines()[-1])
                out("    " + " | ".join(traceback.format_exc().strip().splitlines()[-4:-1]))
        a, b = P["span"]
        out(f"  {sym}: {P['n5']} 5m + {P['n15']} 15m candles {datetime.utcfromtimestamp(a).date()} -> "
            f"{datetime.utcfromtimestamp(b).date()} ({time.time() - t0:.0f}s)")
        del P
        gc.collect()

    out("")
    out("  A = analyse 15m, confirm 5m   B1/B2 = analyse 5m, 15m places the trade")
    out("     version                                 trades  /mo  win%  IS avg  OOS avg  OOS R   PF  maxDD  no-cost  cost")
    out("  " + "-" * 112)
    rows = []
    for grp, name, spec in V:
        tr = sorted(results[(grp, name)], key=lambda x: x["dt"])
        if len(tr) < 30:
            out(f"  {grp:2s} {name:38s} too few trades ({len(tr)})")
            continue
        k = int(len(tr) * 0.6)
        si, so, sa = stats(tr[:k]), stats(tr[k:]), stats(tr)
        rows.append((grp, name, si, so, sa, tr))
        out(f"  {grp:2s} {name:38s} {sa['n']:6d} {sa['pm']:4.0f} {sa['win']:5.1f} {si['avg']:+7.3f} {so['avg']:+8.3f}"
            f" {so['tot']:+6.0f} {so['pf']:4.2f} {so['dd']:6.0f}  {sa['gavg']:+7.3f} {sa['costR']:.2f}R")

    out("")
    out("  OUT-OF-SAMPLE BY PAIR (avg R per trade)")
    for grp, name, si, so, sa, tr in rows:
        oos = tr[int(len(tr) * 0.6):]
        parts = []
        for sym in B.SYMBOLS:
            s = stats([x for x in oos if x["sym"] == sym])
            parts.append(f"{sym.replace('/', '')} " + (f"{s['avg']:+.2f}({s['n']})" if s else "-"))
        out(f"  {grp:2s} {name:38s} " + "  ".join(parts))

    good = [r for r in rows if r[2]["avg"] > 0 and r[3]["avg"] > 0 and r[3]["n"] >= 100]
    out("")
    out("  VERDICT")
    out(f"  versions tested               : {len(rows)}")
    out(f"  positive out-of-sample        : {sum(r[3]['avg'] > 0 for r in rows)}")
    out(f"  positive before costs (all)   : {sum(r[4]['gavg'] > 0 for r in rows)}")
    out(f"  positive in BOTH halves, 100+ : {len(good)}")
    if good:
        b = max(good, key=lambda r: r[3]["avg"])
        out(f"  -> best: {b[0]} {b[1]}  IS {b[2]['avg']:+.3f}R  OOS {b[3]['avg']:+.3f}R over {b[3]['n']} trades")
    else:
        out("  -> no version has an edge in both halves after costs.")

    with open(RESULTS_FILE, "w") as f:
        f.write("\n".join(LOG) + "\n")
    if B.TELEGRAM_BOT_TOKEN and B.TELEGRAM_CHAT_ID:
        lines = [f"{r[0]} {r[1]}: n{r[4]['n']} OOS{r[3]['avg']:+.2f} nocost{r[4]['gavg']:+.2f}" for r in rows]
        msg = ("Fib + BOS, 15m + 5m together, 2015-2026\n" + "\n".join(lines) +
               f"\n\nPositive in both halves: {len(good)} of {len(rows)}")
        try:
            B.requests.post(f"https://api.telegram.org/bot{B.TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": B.TELEGRAM_CHAT_ID, "text": msg}, timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
