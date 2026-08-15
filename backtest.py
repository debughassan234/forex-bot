"""Fib Structure Bot — BACKTEST v9
Settings LOCKED to pivot 3 / leg 2.0 (already validated on the four FX pairs).
This run answers one question: do the index ETFs hold up over the SAME
period as the FX pairs, and in BOTH directions?
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

# LOCKED — no optimising in this run.
GRID_PIVOT = [3]
GRID_MINLEG = [2.0]
GRID_MA = [False]

# ETFs return ~3 years of hourly data (7 trading hours/day) while FX returns
# ~7 months. Comparing across different periods is meaningless, so every
# instrument is trimmed to the window they all share.
MATCH_PERIODS = True

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
        p, c =
