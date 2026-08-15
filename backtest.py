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
    if keys
