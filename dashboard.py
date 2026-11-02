"""Renders the Fib bot dashboard page (served by dash_server.py).

Reads only files the bot already writes on the server:
  status.json         latest read per pair (bias, zone, price, price a signal needs)
  logs/events.jsonl   zone alerts, entry signals, closed trades
  logs/bot.log        run history (health)
  bot_state.json      trades being tracked, last heartbeat
  challenge.json      your FundedNext trades, entered on the page
"""

import html
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
P = lambda *a: os.path.join(BOT_DIR, *a)
LAGOS = timezone(timedelta(hours=1))
PIP = {"EUR/USD": 0.0001, "USD/JPY": 0.01, "EUR/JPY": 0.01, "XAU/USD": 1.0}
PAIRS = ["XAU/USD", "EUR/USD", "USD/JPY", "EUR/JPY"]

# FundedNext Stellar 2-Step
CHALLENGE = {"account": 6000.0, "targets": {1: 8.0, 2: 5.0}, "daily_pct": 5.0,
             "max_pct": 10.0, "min_days": 5, "own_daily_stop": 120.0}


# ------------------------------------------------------------------ helpers
def esc(x):
    return html.escape(str(x), quote=True)


def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def parse_t(s):
    try:
        t = datetime.fromisoformat(s)
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def lagos(t, fmt="%a %d %b %H:%M"):
    return t.astimezone(LAGOS).strftime(fmt) if t else "—"


def ago(t, now):
    if not t:
        return "never"
    m = int((now - t).total_seconds() // 60)
    if m < 1:
        return "just now"
    if m < 60:
        return f"{m} min ago"
    if m < 48 * 60:
        return f"{m // 60} h ago"
    return f"{m // 1440} days ago"


def fmt(sym, x):
    if x is None:
        return "—"
    if "XAU" in sym:
        return f"{x:,.2f}"
    if "JPY" in sym:
        return f"{x:.3f}"
    return f"{x:.5f}"


def pips(sym, d):
    if "XAU" in sym:
        return f"${abs(d):,.2f}"
    return f"{abs(d) / PIP.get(sym, 0.0001):.0f} pips"


def money(x, sign=False):
    s = f"${abs(x):,.2f}"
    if sign:
        return ("+" if x >= 0 else "−") + s
    return ("−" if x < 0 else "") + s


# ------------------------------------------------- FundedNext server day
def _nth_sunday(year, month, n):
    d = datetime(year, month, 1, tzinfo=timezone.utc)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def server_offset(t):
    """FundedNext server: GMT+3 while US daylight saving is on, else GMT+2."""
    start = _nth_sunday(t.year, 3, 2) + timedelta(hours=7)
    end = _nth_sunday(t.year, 11, 1) + timedelta(hours=6)
    return 3 if start <= t < end else 2


def server_day(t):
    return (t + timedelta(hours=server_offset(t))).date()


def next_reset(now):
    off = server_offset(now)
    local = now + timedelta(hours=off)
    midnight = datetime(local.year, local.month, local.day, tzinfo=timezone.utc) + timedelta(days=1)
    return midnight - timedelta(hours=off)


# ------------------------------------------------------------- challenge
def load_challenge():
    c = read_json(P("challenge.json"), None)
    if not isinstance(c, dict):
        c = {"phase": 1, "trades": [], "history": []}
    c.setdefault("phase", 1)
    c.setdefault("trades", [])
    c.setdefault("history", [])
    return c


def save_challenge(c):
    tmp = P("challenge.json.tmp")
    with open(tmp, "w") as f:
        json.dump(c, f, indent=1)
    os.replace(tmp, P("challenge.json"))


def apply_action(a):
    """Changes from the page. Returns (ok, message)."""
    c = load_challenge()
    act = a.get("action")
    if act == "add":
        try:
            pnl = round(float(a.get("pnl")), 2)
        except (TypeError, ValueError):
            return False, "Enter the profit or loss as a number, e.g. 54.20 or -29.80"
        if not -10000 < pnl < 10000:
            return False, "That amount looks wrong"
        pair = a.get("pair") if a.get("pair") in PAIRS + ["Other"] else "Other"
        side = a.get("side") if a.get("side") in ("Buy", "Sell") else ""
        t = None
        if a.get("closed"):
            try:   # datetime-local from the page, in Lagos time
                t = datetime.strptime(a["closed"][:16], "%Y-%m-%dT%H:%M").replace(tzinfo=LAGOS)
            except ValueError:
                return False, "Closing time not understood"
        t = (t or datetime.now(timezone.utc)).astimezone(timezone.utc)
        note = str(a.get("note") or "")[:120]
        c["trades"].append({"id": uuid.uuid4().hex[:10], "t": t.isoformat(timespec="minutes"),
                            "pair": pair, "side": side, "pnl": pnl, "note": note})
        c["trades"].sort(key=lambda x: x["t"])
        save_challenge(c)
        return True, "Trade added"
    if act == "delete":
        before = len(c["trades"])
        c["trades"] = [x for x in c["trades"] if x.get("id") != a.get("id")]
        save_challenge(c)
        return before != len(c["trades"]), "Trade removed"
    if act == "phase":
        try:
            ph = int(a.get("phase"))
        except (TypeError, ValueError):
            return False, "Bad phase"
        if ph not in (1, 2, 3):
            return False, "Bad phase"
        c["history"].append({"phase": c["phase"], "ended": datetime.now(timezone.utc).isoformat(timespec="minutes"),
                             "trades": c["trades"]})
        c["phase"], c["trades"] = ph, []
        save_challenge(c)
        return True, f"Now tracking phase {ph}"
    return False, "Unknown action"


def challenge_view(now):
    c = load_challenge()
    acc = CHALLENGE["account"]
    phase = c["phase"]
    funded = phase == 3
    target = 0 if funded else acc * CHALLENGE["targets"][phase] / 100
    trades = c["trades"]
    profit = sum(x["pnl"] for x in trades)
    balance = acc + profit
    floor = acc * (1 - CHALLENGE["max_pct"] / 100)
    daily_limit = acc * CHALLENGE["daily_pct"] / 100
    today = server_day(now)
    today_pnl = sum(x["pnl"] for x in trades if (t := parse_t(x["t"])) and server_day(t) == today)
    used = max(0.0, -today_pnl)
    days = len({server_day(t) for x in trades if (t := parse_t(x["t"]))})
    return {"phase": phase, "funded": funded, "target": target, "profit": profit, "balance": balance,
            "floor": floor, "room": balance - floor, "daily_limit": daily_limit, "today_pnl": today_pnl,
            "used": used, "days": days, "trades": trades, "reset": next_reset(now),
            "wins": sum(x["pnl"] > 0 for x in trades), "losses": sum(x["pnl"] < 0 for x in trades)}


# ------------------------------------------------------------------ health
def health(now):
    runs, hiccups, tg_err, last = [], 0, 0, None
    hour_ago = now - timedelta(hours=1)
    cur = None
    try:
        with open(P("logs", "bot.log"), errors="replace") as f:
            lines = f.readlines()[-6000:]
    except Exception:
        lines = []
    for ln in lines:
        m = re.match(r"=== (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) UTC ===", ln)
        if m:
            cur = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            last = cur
            if cur >= hour_ago:
                runs.append(cur)
            continue
        if cur and cur >= hour_ago:
            if re.search(r"HTTP 5\d\d", ln):
                hiccups += 1
            if "telegram" in ln:
                tg_err += 1
    st = read_json(P("bot_state.json"), {})
    age = (now - last).total_seconds() / 60 if last else None
    ok = age is not None and age <= 3 and tg_err == 0
    return {"last": last, "age": age, "runs": len(runs), "hiccups": hiccups, "tg_err": tg_err,
            "heartbeat": st.get("heartbeat_day"), "open": st.get("open_trades", []), "ok": ok}


def events(n=25):
    out = []
    try:
        with open(P("logs", "events.jsonl")) as f:
            for ln in f.readlines()[-400:]:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except Exception:
        pass
    return out[-n:][::-1], out


# ------------------------------------------------------------------ render
CSS = """
:root{
  --ink:#0b0c0e; --ink2:rgba(11,12,14,.6); --ink3:rgba(11,12,14,.36);
  --bg1:#dde0e5; --bg2:#b3b8c1; --orb1:rgba(255,255,255,1); --orb2:rgba(62,68,78,.42); --orb3:rgba(255,255,255,.9);
  --tint:rgba(255,255,255,.04); --tint2:rgba(255,255,255,.12);
  --rim1:rgba(255,255,255,1); --rim2:rgba(255,255,255,.15); --rim3:rgba(255,255,255,.65);
  --sheen:rgba(255,255,255,.42); --inner:rgba(255,255,255,.28);
  --shadow:0 18px 50px rgba(30,34,40,.16), 0 2px 6px rgba(30,34,40,.08);
  --line:rgba(11,12,14,.08); --track:rgba(11,12,14,.09); --fill:rgba(11,12,14,.8);
}
@media (prefers-color-scheme:dark){:root{
  --ink:#f5f6f8; --ink2:rgba(245,246,248,.62); --ink3:rgba(245,246,248,.36);
  --bg1:#07080a; --bg2:#121418; --orb1:rgba(255,255,255,.22); --orb2:rgba(255,255,255,.07); --orb3:rgba(255,255,255,.14);
  --tint:rgba(255,255,255,.035); --tint2:rgba(255,255,255,.05);
  --rim1:rgba(255,255,255,.55); --rim2:rgba(255,255,255,.05); --rim3:rgba(255,255,255,.28);
  --sheen:rgba(255,255,255,.12); --inner:rgba(255,255,255,.06);
  --shadow:0 20px 60px rgba(0,0,0,.6), 0 2px 6px rgba(0,0,0,.5);
  --line:rgba(255,255,255,.08); --track:rgba(255,255,255,.1); --fill:rgba(255,255,255,.9);
}}
*{box-sizing:border-box}
html{background:var(--bg1)}
body{margin:0;color:var(--ink);min-height:100vh;position:relative;overflow-x:hidden;
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,sans-serif;
  -webkit-font-smoothing:antialiased;background:linear-gradient(165deg,var(--bg1),var(--bg2)) fixed}
/* soft light shapes behind the glass, so the clear panels have something to bend */
body::before,body::after{content:"";position:fixed;z-index:-1;border-radius:50%;filter:blur(60px);pointer-events:none}
body::before{width:70vmax;height:70vmax;left:-25vmax;top:-30vmax;
  background:radial-gradient(circle at 40% 40%,var(--orb1),transparent 62%)}
body::after{width:60vmax;height:60vmax;right:-22vmax;top:25vh;
  background:radial-gradient(circle at 50% 50%,var(--orb2),transparent 64%),
             radial-gradient(circle at 20% 90%,var(--orb3),transparent 50%)}
main{max-width:860px;margin:0 auto;padding:18px 16px 56px;position:relative}
h1{font-size:28px;letter-spacing:-.025em;margin:6px 0 4px;font-weight:700;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
h2{font-size:12px;margin:0 0 12px;color:var(--ink2);font-weight:600;text-transform:uppercase;letter-spacing:.1em}
.sub{color:var(--ink2);font-size:13px}

/* ---------- crystal glass ---------- */
.card,.stat,.pill,input,select,button{position:relative;isolation:isolate;
  background:linear-gradient(135deg,var(--tint2),var(--tint));
  -webkit-backdrop-filter:blur(7px) saturate(170%) brightness(1.06);
  backdrop-filter:blur(7px) saturate(170%) brightness(1.06);
  border:0;box-shadow:inset 0 0 0 1px var(--rim2),inset 0 1px 18px var(--inner),var(--shadow)}
/* bright refracted rim: strongest top-left and bottom-right */
.card::before,.stat::before,.pill::before,button::before{content:"";position:absolute;inset:0;border-radius:inherit;
  padding:1.3px;pointer-events:none;z-index:1;
  background:linear-gradient(135deg,var(--rim1) 0%,var(--rim2) 32%,var(--rim2) 62%,var(--rim3) 100%);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);-webkit-mask-composite:xor;
  mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);mask-composite:exclude}
/* light sheen across the top, like light on a curved surface */
.card::after,.stat::after{content:"";position:absolute;left:1px;right:1px;top:1px;height:46%;border-radius:inherit;
  border-bottom-left-radius:50% 22px;border-bottom-right-radius:50% 22px;pointer-events:none;z-index:-1;
  background:linear-gradient(180deg,var(--sheen),transparent)}
.card{border-radius:28px;padding:17px 18px;margin:16px 0}
.stat{border-radius:18px;padding:12px 14px;box-shadow:inset 0 0 0 1px var(--rim2),inset 0 1px 12px var(--inner)}
.row{display:flex;gap:10px;flex-wrap:wrap}.row>*{flex:1 1 150px}
.stat b{display:block;font-size:21px;letter-spacing:-.015em;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--ink2)}
.pill{display:inline-flex;align-items:center;gap:5px;padding:3px 11px;border-radius:99px;font-size:12px;font-weight:600;
  color:var(--ink);box-shadow:inset 0 0 0 1px var(--rim2)}
.good{font-weight:700}.mute{color:var(--ink2)}
.bad{font-weight:700;text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}
.warn{font-weight:700;text-decoration:underline dotted;text-underline-offset:3px}
.pill.bad,.pill.warn{text-decoration:none;font-weight:700;box-shadow:inset 0 0 0 1.5px var(--ink2)}
.bar{height:7px;background:var(--track);border-radius:99px;overflow:hidden;margin:8px 0 3px;
  box-shadow:inset 0 1px 2px rgba(0,0,0,.08)}
.bar i{display:block;height:100%;background:var(--fill);border-radius:99px}
.pair{border-top:1px solid var(--line);padding:13px 0}.pair:first-of-type{border-top:0;padding-top:0}
.pair h3{margin:0;font-size:17px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;letter-spacing:-.01em}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:14px;margin-top:8px}
.kv dt{color:var(--ink2)}.kv dd{margin:0;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:8px 4px;border-top:1px solid var(--line)}
th{color:var(--ink2);font-weight:500;font-size:12px;border-top:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}
td.good,td.bad{text-decoration:none;font-weight:600}td.bad{color:var(--ink2)}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-top:10px}
input,select,button{font:inherit;padding:10px 13px;border-radius:14px;min-width:0;width:100%;color:var(--ink);outline:none;
  box-shadow:inset 0 0 0 1px var(--rim2),inset 0 1px 8px var(--inner)}
input:focus,select:focus{box-shadow:inset 0 0 0 1.5px var(--ink3),inset 0 1px 8px var(--inner)}
input::placeholder{color:var(--ink3)}
select{-webkit-appearance:none;appearance:none;
  background-image:linear-gradient(135deg,var(--tint2),var(--tint)),
    linear-gradient(45deg,transparent 50%,var(--ink2) 50%),linear-gradient(135deg,var(--ink2) 50%,transparent 50%);
  background-position:0 0,calc(100% - 17px) 52%,calc(100% - 12px) 52%;
  background-size:100% 100%,5px 5px,5px 5px;background-repeat:no-repeat;padding-right:30px}
button{font-weight:700;cursor:pointer;letter-spacing:.01em;box-shadow:inset 0 0 0 1px var(--rim2),inset 0 1px 14px var(--inner),var(--shadow)}
button:active{transform:scale(.98)}
button.ghost{padding:5px 11px;width:auto;font-size:12px;font-weight:600;
  box-shadow:inset 0 0 0 1px var(--rim2),inset 0 1px 8px var(--inner)}
.msg{font-size:13px;margin-top:6px}
.ev{font-size:14px;padding:8px 0;border-top:1px solid var(--line)}.ev:first-child{border-top:0}
.tag{display:inline-block;min-width:58px;font-size:11px;font-weight:700;letter-spacing:.06em;color:var(--ink2)}
details summary{cursor:pointer;color:var(--ink2);font-size:13px}
@supports not ((backdrop-filter:blur(1px)) or (-webkit-backdrop-filter:blur(1px))){
  .card{background:rgba(255,255,255,.55)}}
"""


def render(now=None):
    now = now or datetime.now(timezone.utc)
    st = read_json(P("status.json"), {})
    h = health(now)
    ch = challenge_view(now)
    recent, all_ev = events()
    acc = CHALLENGE["account"]

    # header
    state = ("<span class='pill'>● Running</span>" if h["ok"] else
             "<span class='pill bad'>⚠︎ Check the bot</span>")
    parts = [f"<h1>Fib Bot {state}</h1>",
             f"<div class='sub'>Updated {esc(lagos(now, '%H:%M:%S'))} Lagos · last check "
             f"{esc(ago(h['last'], now))} · refreshes every minute</div>"]

    # challenge
    if ch["funded"]:
        title = "FundedNext · funded account"
        prog = ""
    else:
        pct = max(0, min(100, 100 * ch["profit"] / ch["target"])) if ch["target"] else 0
        left = ch["target"] - ch["profit"]
        prog = (f"<div class='sub'>Profit target {money(ch['target'])} · "
                f"{'<b class=good>reached</b>' if left <= 0 else money(left) + ' to go'}</div>"
                f"<div class='bar'><i style='width:{pct:.0f}%'></i></div>")
        title = f"FundedNext 2-Step · Phase {ch['phase']}"
    used_pct = min(100, 100 * ch["used"] / ch["daily_limit"])
    own = CHALLENGE["own_daily_stop"]
    dclass = "bad" if ch["used"] >= own else ("warn" if ch["used"] >= own / 2 else "good")
    days_ok = ch["days"] >= CHALLENGE["min_days"]
    done = (not ch["funded"] and ch["profit"] >= ch["target"] and days_ok)
    parts.append(f"<section class='card'><h2>{esc(title)}</h2>{prog}<div class='row'>"
                 f"<div class='stat'><b>{money(ch['balance'])}</b><span>balance ({money(ch['profit'], True)})</span></div>"
                 f"<div class='stat'><b class='{dclass}'>{money(ch['used'])} / {money(ch['daily_limit'])}</b>"
                 f"<span>lost today · your stop {money(own)}</span>"
                 f"<div class='bar'><i style='width:{used_pct:.0f}%'></i></div></div>"
                 f"<div class='stat'><b>{money(ch['room'])}</b><span>room above the {money(ch['floor'])} floor</span></div>"
                 f"<div class='stat'><b class='{'good' if days_ok else ''}'>{ch['days']} / {CHALLENGE['min_days']}</b>"
                 f"<span>trading days</span></div></div>"
                 f"<div class='sub' style='margin-top:8px'>Daily limit resets {esc(lagos(ch['reset'], '%a %H:%M'))} Lagos · "
                 f"{ch['wins']} win{'' if ch['wins'] == 1 else 's'}, {ch['losses']} loss{'' if ch['losses'] == 1 else 'es'}</div>"
                 + ("<p><b>✓ Phase target and trading days met — check your FundedNext dashboard.</b></p>" if done else "")
                 + _trade_form(ch) + "</section>")

    # pairs
    rows = []
    stp = {p["sym"]: p for p in st.get("pairs", [])}
    st_t = parse_t(st.get("t", ""))
    for sym in PAIRS:
        p = stp.get(sym, {})
        bias = p.get("bias")
        line = p.get("line", "no data yet").split(": ", 1)[-1]
        badge = ""
        if p.get("in_zone"):
            badge = "<span class='pill warn'>in zone</span>"
        if bias:
            badge = f"<span class='pill'>{'▲ bullish' if bias == 'bullish' else '▼ bearish'}</span>" + badge
        kv = ""
        z = p.get("zone")
        price = p.get("price")
        if z:
            need = p.get("need")
            side = "SELL" if bias == "bearish" else "BUY"
            reach = (price >= need) if bias == "bearish" else (price <= need)
            dist = (f"<span class='good'>reached</span>" if reach
                    else f"{pips(sym, need - price)} away")
            kv = (f"<dl class='kv'><dt>Price</dt><dd>{fmt(sym, price)}</dd>"
                  f"<dt>Golden zone</dt><dd>{fmt(sym, z['z_bot'])} – {fmt(sym, z['z_top'])}</dd>"
                  f"<dt>{side} needs</dt><dd>{fmt(sym, need)} {'or higher' if side == 'SELL' else 'or lower'} · {dist}</dd>"
                  f"<dt>Plan</dt><dd>SL {fmt(sym, z['sl'])} · TP1 {fmt(sym, z['tp1'])} · TP2 {fmt(sym, z['tp2'])}</dd></dl>")
        elif price:
            kv = f"<dl class='kv'><dt>Price</dt><dd>{fmt(sym, price)}</dd></dl>"
        rows.append(f"<div class='pair'><h3>{esc(sym)} {badge}</h3><div class='sub'>{esc(line)}</div>{kv}</div>")
    parts.append(f"<section class='card'><h2>Pairs</h2>{''.join(rows)}"
                 f"<div class='sub' style='margin-top:8px'>As of {esc(lagos(st_t, '%H:%M'))} Lagos</div></section>")

    # alerts and trades
    open_rows = "".join(
        f"<tr><td>{esc(t['sym'])}</td><td>{'Buy' if t['dir'] == 'bullish' else 'Sell'}</td>"
        f"<td class='num'>{fmt(t['sym'], t['entry'])}</td><td class='num'>{fmt(t['sym'], t['sl'])}</td>"
        f"<td class='num'>{fmt(t['sym'], t['tp1'])}{' ✓' if t.get('hit_tp1') else ''}</td></tr>"
        for t in h["open"])
    open_html = (f"<table><tr><th>Pair</th><th>Side</th><th class='num'>Entry</th><th class='num'>SL</th>"
                 f"<th class='num'>TP1</th></tr>{open_rows}</table>" if open_rows
                 else "<div class='sub'>No signals being tracked right now.</div>")
    ev_html = []
    for e in recent:
        t = parse_t(e.get("t", ""))
        sym = e.get("sym", "")
        if e["kind"] == "armed":
            txt = (f"<span class='tag'>ZONE</span><b>{esc(sym)}</b> zone armed ({'sell' if e.get('dir') == 'bearish' else 'buy'} side) · "
                   f"signal needs {fmt(sym, e.get('need'))}")
        elif e["kind"] == "entry":
            txt = (f"<span class='tag'>{'▼ SELL' if e.get('dir') == 'bearish' else '▲ BUY'}</span><b>{esc(sym)}</b> at {fmt(sym, e.get('price'))} · "
                   f"SL {fmt(sym, e.get('sl'))} · TP1 {fmt(sym, e.get('tp1'))} · {esc(e.get('lots'))} lots · RR {esc(e.get('rr'))}")
        elif e["kind"] == "closed":
            r = e.get("R") or 0
            txt = (f"<span class='tag'>{'✓ WIN' if r > 0 else '✕ LOSS'}</span><b>{esc(sym)}</b> closed: {esc(e.get('result'))} "
                   f"<span class='{'good' if r > 0 else 'bad'}'>{r:+.2f}R</span>")
        else:
            continue
        ev_html.append(f"<div class='ev'><span class='mute'>{esc(lagos(t))}</span><br>{txt}</div>")
    closed = [e for e in all_ev if e.get("kind") == "closed"]
    tot = sum(e.get("R") or 0 for e in closed)
    summary = (f"<div class='sub'>Signals closed: {len(closed)} · total {tot:+.2f}R</div>" if closed else "")
    parts.append(f"<section class='card'><h2>Signals being tracked</h2>{open_html}</section>"
                 f"<section class='card'><h2>Recent alerts</h2>{summary}"
                 f"{''.join(ev_html) or '<div class=sub>No alerts recorded yet.</div>'}</section>")

    # health
    age = h["age"]
    age_cls = "good" if age is not None and age <= 3 else "bad"
    parts.append(
        "<section class='card'><h2>Bot health</h2><div class='row'>"
        f"<div class='stat'><b class='{age_cls}'>{esc(ago(h['last'], now))}</b><span>last check</span></div>"
        f"<div class='stat'><b class='{'good' if h['runs'] >= 50 else 'warn'}'>{h['runs']} / 60</b><span>checks last hour</span></div>"
        f"<div class='stat'><b class='{'good' if h['hiccups'] < 30 else 'warn'}'>{h['hiccups']}</b><span>OANDA hiccups last hour (auto-retried)</span></div>"
        f"<div class='stat'><b class='{'good' if h['tg_err'] == 0 else 'bad'}'>{'OK' if h['tg_err'] == 0 else h['tg_err']}</b>"
        f"<span>Telegram{'' if h['tg_err'] == 0 else ' errors'}</span></div></div>"
        f"<div class='sub' style='margin-top:8px'>Last heartbeat {esc(h['heartbeat'] or 'none yet')} · "
        f"risk {esc(st.get('risk', '—'))}% on {money(st.get('account', acc))} · min reward:risk {esc(st.get('min_rr', '—'))}</div>"
        "</section>")

    return PAGE.replace("{{CSS}}", CSS).replace("{{BODY}}", "".join(parts))


def _trade_form(ch):
    nowl = datetime.now(LAGOS).strftime("%Y-%m-%dT%H:%M")
    rows = "".join(
        f"<tr><td>{esc(lagos(parse_t(x['t']), '%d %b %H:%M'))}</td><td>{esc(x['pair'])} {esc(x.get('side', ''))}"
        f"{'<br><span class=sub>' + esc(x['note']) + '</span>' if x.get('note') else ''}</td>"
        f"<td class='num {'good' if x['pnl'] > 0 else 'bad'}'>{money(x['pnl'], True)}</td>"
        f"<td class='num'><button class='ghost' onclick=\"act({{action:'delete',id:'{esc(x['id'])}'}},'Remove this trade?')\">✕</button></td></tr>"
        for x in reversed(ch["trades"]))
    table = (f"<table style='margin-top:12px'><tr><th>Closed (Lagos)</th><th>Trade</th><th class='num'>P/L</th><th></th></tr>"
             f"{rows}</table>") if rows else "<div class='sub' style='margin-top:10px'>No trades entered yet.</div>"
    nxt = {1: (2, "I passed Phase 1 → track Phase 2"), 2: (3, "I'm funded → track funded account"),
           3: (1, "Start a new challenge")}[ch["phase"]]
    return (
        "<details style='margin-top:12px' open><summary>Add a closed trade from MT5</summary>"
        "<form id='tf' onsubmit='addTrade(event)'>"
        f"<select name='pair'>{''.join(f'<option>{p}</option>' for p in PAIRS + ['Other'])}</select>"
        "<select name='side'><option>Sell</option><option>Buy</option></select>"
        "<input name='pnl' inputmode='decimal' placeholder='P/L $, e.g. -29.80' required>"
        f"<input name='closed' type='datetime-local' value='{nowl}'>"
        "<input name='note' maxlength='120' placeholder='Note (optional)'>"
        "<button>Add trade</button></form><div id='msg' class='msg'></div></details>"
        + table +
        f"<div style='margin-top:12px'><button class='ghost' onclick=\"act({{action:'phase',phase:{nxt[0]}}},"
        f"'This archives the current trades. Continue?')\">{esc(nxt[1])}</button></div>")


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow"><title>Fib Bot</title>
<style>{{CSS}}</style></head><body><main>{{BODY}}</main>
<script>
async function act(body, ask){
  if(ask && !confirm(ask)) return;
  const r = await fetch('api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const j = await r.json().catch(()=>({ok:false,msg:'Server error'}));
  if(j.ok) location.reload(); else alert(j.msg || 'Failed');
}
async function addTrade(e){
  e.preventDefault();
  const f = new FormData(e.target), body = {action:'add'};
  f.forEach((v,k)=>body[k]=v);
  body.pnl = String(body.pnl).replace(/[$,\\s]/g,'').replace('\\u2212','-');
  const r = await fetch('api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  const j = await r.json().catch(()=>({ok:false,msg:'Server error'}));
  if(j.ok) location.reload(); else document.getElementById('msg').textContent = j.msg;
}
setInterval(()=>{
  const a = document.activeElement, f = document.getElementById('tf');
  const typing = a && ['INPUT','SELECT'].includes(a.tagName);
  const filled = f && (f.pnl.value || f.note.value);
  if(!typing && !filled) location.reload();
}, 60000);
</script></body></html>"""


if __name__ == "__main__":
    print(render())
