"""FundedNext Stellar 2-Step — would the bot have passed?

Takes the bot's real 2015-2026 trades (Daily > 4H > 1H, live rules, OANDA
data, costs included) and replays them through the 2-Step rules, starting
a new challenge at the beginning of every month:

  Phase 1: +8%   Phase 2: +5%   (no time limit, at least 5 trading days each)
  Daily loss: 5% of the initial balance, plus that day's realised profit
  Max loss:   balance may never fall below 90% of the initial balance

Each trade is taken at a fixed % risk of the INITIAL balance (like the bot's
lot sizes), split 50/50 at TP1/TP2 as in the backtest. Losses are booked
when a trade closes; open-trade (floating) losses are not modelled, so real
daily-loss breaches would be slightly MORE likely than shown here.
"""

import os
import time
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

import backtest_stacks as B

RISKS = [0.5, 1.0, 1.5, 2.0]
TARGET1, TARGET2 = 8.0, 5.0
DAILY_LOSS, MAX_LOSS = 5.0, 10.0
MIN_DAYS = 5
HORIZON_MONTHS = 24          # stop watching a challenge after 2 years
FEE_6K = 59.99

LOG = []


def out(line=""):
    print(line, flush=True)
    LOG.append(line)


def load_trades():
    data = {}
    for sym in B.SYMBOLS:
        d = {}
        for iv in ("1day", "4h", "1h"):
            c = B.fetch_oanda(sym, iv)
            if not c or len(c) < 200:
                d = None
                break
            d[iv] = c
        if not d:
            out(f"  {sym}: insufficient data")
            continue
        d["sw"] = {(iv, B.PIVOT): B.Sw(d[iv], B.PIVOT) for iv in ("1day", "4h", "1h")}
        d["sw"][("1h", B.PIVOT_ENTRY)] = B.Sw(d["1h"], B.PIVOT_ENTRY)
        decide = [c["dt"] + B.TF["1h"] for c in d["1h"]]
        d["map"] = {("1h", tf): B.closed_index(d[tf], tf, decide) for tf in ("1day", "4h")}
        data[sym] = d
        out(f"  {sym}: {len(d['1h'])} x 1h  {d['1h'][0]['dt'].date()} -> {d['1h'][-1]['dt'].date()}")
    stack = ("A  Daily > 4H > 1H", "1day", "4h", None, "1h")
    return B.run_stack(data, stack, "standard")


def phase(trades, i, target, risk):
    """Run one phase from trade index i. Returns (result, next_index, end_time)."""
    bal, days = 0.0, set()
    day_pnl = defaultdict(float)
    day_low = defaultdict(float)
    order = sorted(trades[i:], key=lambda x: x["exit"])
    for t in order:
        days.add(t["dt"].date())
        pnl = t["R"] * risk
        d = t["exit"].date()
        day_pnl[d] += pnl
        day_low[d] = min(day_low[d], day_pnl[d])
        bal += pnl
        if day_low[d] <= -DAILY_LOSS:
            return "fail-daily", t, t["exit"]
        if bal <= -MAX_LOSS:
            return "fail-max", t, t["exit"]
        if bal >= target and len(days) >= MIN_DAYS:
            return "pass", t, t["exit"]
    return "open", None, None


def challenge(trades, start, risk):
    horizon = start + timedelta(days=30.4 * HORIZON_MONTHS)
    pool = [t for t in trades if start <= t["dt"] < horizon]
    if not pool:
        return "open", None
    res, last, end = phase(pool, 0, TARGET1, risk)
    if res != "pass":
        return ("phase1-" + res) if res != "open" else "open", end
    rest = [t for t in pool if t["dt"] > last["exit"]]
    if not rest:
        return "open", None
    res, last, end2 = phase(rest, 0, TARGET2, risk)
    if res == "pass":
        return "passed", end2
    return ("phase2-" + res) if res != "open" else "open", end2


def main():
    out(f"FUNDEDNEXT STELLAR 2-STEP SIMULATION · run {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    trades = load_trades()
    if not trades:
        out("No trades — check OANDA_API_KEY.")
        return finish()
    s = B.stats(trades)
    out(f"\n  bot trades 2015-2026: {s['n']}  avg {s['avgR']:+.3f}R  win {s['win']:.1f}%  "
        f"{s['permonth']:.1f}/month")
    gold = [t for t in trades if t["sym"] == "XAU/USD"]

    for label, tr in (("ALL 4 PAIRS (the live bot)", trades), ("GOLD ONLY", gold)):
        if len(tr) < 20:
            continue
        g = B.stats(tr)
        first = tr[0]["dt"].replace(day=1, hour=0, minute=0)
        last_start = tr[-1]["dt"] - timedelta(days=30.4 * HORIZON_MONTHS)   # full window only
        starts = []
        d = first
        while d <= last_start:
            starts.append(d)
            d = (d + timedelta(days=32)).replace(day=1)
        out(f"\n  {label}: {g['n']} trades, avg {g['avgR']:+.3f}R, {g['permonth']:.1f}/month, "
            f"{len(starts)} monthly start dates")
        out(f"  {'risk/trade':>10s} {'PASSED':>7s} {'failed P1':>9s} {'failed P2':>9s} "
            f"{'daily-loss':>10s} {'unfinished':>10s} {'median time':>11s} {'cost per pass':>13s}")
        for risk in RISKS:
            outcomes, months = defaultdict(int), []
            for st in starts:
                res, end = challenge(tr, st, risk)
                key = ("passed" if res == "passed" else
                       "p1" if res.startswith("phase1") else
                       "p2" if res.startswith("phase2") else "open")
                outcomes[key] += 1
                if "daily" in res:
                    outcomes["daily"] += 1
                if res == "passed":
                    months.append((end - st).days / 30.4)
            n = len(starts)
            pr = outcomes["passed"] / n
            med = f"{statistics.median(months):.1f} mo" if months else "-"
            cost = f"${FEE_6K / pr:,.0f}" if pr > 0 else "never"
            out(f"  {risk:>9.1f}% {100*pr:6.1f}% {100*outcomes['p1']/n:8.1f}% "
                f"{100*outcomes['p2']/n:8.1f}% {100*outcomes['daily']/n:9.1f}% "
                f"{100*outcomes['open']/n:9.1f}% {med:>11s} {cost:>13s}")

    out("\n  How to read: each row starts a fresh challenge at the start of every month")
    out("  from 2015 and follows the bot's real trades. 'unfinished' = neither passed nor")
    out(f"  failed within {HORIZON_MONTHS} months. 'cost per pass' = $6K fee / pass rate.")
    out("  Start dates overlap in time, so treat the percentages as rough odds.")
    finish()


def finish():
    with open("prop_sim_results.txt", "w") as f:
        f.write("\n".join(LOG) + "\n")
    if B.TELEGRAM_BOT_TOKEN and B.TELEGRAM_CHAT_ID:
        body = "\n".join(l for l in LOG if l.startswith("  ") and ("%" in l or ":" in l))[-3500:]
        try:
            B.requests.post(f"https://api.telegram.org/bot{B.TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": B.TELEGRAM_CHAT_ID,
                                  "text": "FundedNext 2-Step simulation\n\n" + body},
                            timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
