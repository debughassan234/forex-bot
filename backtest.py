"""
Fib Structure Bot — BACKTEST ENGINE v3

Four surviving pairs, one proven speed (Daily -> 4H -> 1H).
Tests pivot (2,3,4) x min_leg_atr (1.0 - 2.5) with the MA fallback off.
Reports a robustness check (plateau vs curve-fit spike) and the
FundedNext challenge math in percent terms.

Your fib settings are fixed and untouched:
zone 0.382-0.618, SL beyond 1.0 + buffer, TP1 -0.382, TP2 -0.618.
"""

import os
import time
import bisect
import json
import requests
from datetime import datetime, timedelta

API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# The four survivors. GBP/JPY dropped (negative in two independent runs),
# AUD/USD and USD/CAD dropped (negative), XAG/USD needs a paid data plan.
SYMBOLS = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]

COST = {"XAU/USD": 0.35, "EUR/USD": 0.00012, "USD/JPY": 0.015, "EUR/JPY": 0.020}

# FAST (4H->1H->15min) was tested: 96 trades, -0.2R. Dropped.
SPEEDS = {
    "Daily -> 4H -> 1H": {"bias": "1day", "zone": "4h", "entry": "1h",
                          "bias_n": 1500, "zone_n": 5000, "entry_n": 5000,
                          "bars_max": 240},
}

ZONE_LOW, ZONE_HIGH, ZONE_PRIME = 0.382, 0.618, 0.5
SL_BUFFER, TP1_EXT, TP2_EXT = 0.10, 0.382, 0.618
PIVOT_ENTRY = 2
ZONE_LOOKBACK = 12
MA_PERIOD = 50

MIN_RR   = 1.5
ZONE_TOL = 0.10

GRID_PIVOT = [2, 3, 4]
GRID_MINLEG = [1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5]
GRID_MA = [False]        # settled: MA fallback lost money in every test

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


def atr_at(candles,
