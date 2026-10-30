"""Does a lower (or higher) minimum reward:risk help?

Same live-bot rules (Daily > 4H > 1H, standard triggers), OANDA 2015-2026,
costs included. Only MIN_RR changes. First 60% of trades = in-sample,
last 40% = out-of-sample. Also replays each version through the
FundedNext 2-Step rules (monthly starts, 24-month window) at 1% and 2% risk.
"""
import time
from datetime import timedelta

import backtest_stacks as B
import prop_sim as P

LEVELS = [1.0, 1.2, 1.5, 2.0]
LOG = []


def out(line=""):
    print(line, flush=True)
    LOG.append(line)


def load():
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
    return data


def pass_rate(trades, risk):
    first = trades[0]["dt"].replace(day=1, hour=0, minute=0)
    last = trades[-1]["dt"] - timedelta(days=30.4 * P.HORIZON_MONTHS)
    starts, d = [], first
    while d <= last:
        starts.append(d)
        d = (d + timedelta(days=32)).replace(day=1)
    res = [P.challenge(trades, s, risk)[0] for s in starts]
    passed = sum(r == "passed" for r in res)
    failed = sum(r.startswith(("phase1-fail", "phase2-fail")) for r in res)
    return 100.0 * passed / len(res), 100.0 * failed / len(res)


def main():
    out(f"MIN REWARD:RISK TEST · run {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC")
    data = load()
    if not data:
        out("No data — check OANDA_API_KEY.")
        return finish()
    stack = ("A  Daily > 4H > 1H", "1day", "4h", None, "1h")
    out("")
    out(f"  {'min RR':>6s} {'trades':>6s} {'/mo':>4s} {'IS avg':>7s} {'OOS avg':>8s} {'OOS R':>7s} "
        f"{'win%':>5s} {'PF':>5s} {'pass@1%':>8s} {'pass@2%':>8s} {'fail@2%':>8s}")
    out("  " + "-" * 88)
    for rr in LEVELS:
        B.MIN_RR = rr
        tr = B.run_stack(data, stack, "standard")
        if len(tr) < 20:
            out(f"  {rr:>6.1f}  too few trades")
            continue
        k = int(len(tr) * (1 - B.OOS_FRACTION))
        si, so, sa = B.stats(tr[:k]), B.stats(tr[k:]), B.stats(tr)
        p1, _ = pass_rate(tr, 1.0)
        p2, f2 = pass_rate(tr, 2.0)
        mark = "  <- current" if rr == 1.5 else ""
        out(f"  {rr:>6.1f} {sa['n']:>6d} {sa['permonth']:>4.1f} {si['avgR']:>+7.3f} {so['avgR']:>+8.3f} "
            f"{so['totR']:>+7.1f} {so['win']:>5.1f} {so['pf']:>5.2f} {p1:>7.1f}% {p2:>7.1f}% {f2:>7.1f}%{mark}")
    out("")
    out("  IS/OOS avg = average R per trade in the first 60% / last 40% of trades.")
    out("  pass@ = share of monthly challenge starts that passed both phases within 24 months.")
    finish()


def finish():
    with open("rr_test_results.txt", "w") as f:
        f.write("\n".join(LOG) + "\n")
    if B.TELEGRAM_BOT_TOKEN and B.TELEGRAM_CHAT_ID:
        try:
            B.requests.post(f"https://api.telegram.org/bot{B.TELEGRAM_BOT_TOKEN}/sendMessage",
                            json={"chat_id": B.TELEGRAM_CHAT_ID,
                                  "text": "Min reward:risk test 2015-2026\n\n" + "\n".join(LOG[2:])},
                            timeout=20)
        except Exception as e:
            print(f"[warn] telegram: {e}")


if __name__ == "__main__":
    main()
