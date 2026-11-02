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
BG_FILE = P("assets", "user_bg.jpg")   # your own background photo (never uploaded to GitHub)

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
    if act == "bg_remove":
        try:
            os.remove(BG_FILE)
        except OSError:
            pass
        return True, "Background removed"
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
ICON = {  # small monochrome line icons
    "wallet": '<path d="M3 7h15a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7zm0 0V6a2 2 0 0 1 2-2h11"/><circle cx="16" cy="13" r="1.2"/>',
    "target": '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
    "shield": '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6l7-3z"/><path d="M9 12h6"/>',
    "cal": '<rect x="4" y="5" width="16" height="15" rx="2"/><path d="M4 10h16M9 3v4M15 3v4"/>',
    "floor": '<path d="M4 18h16"/><path d="M6 14l4-4 3 3 5-6"/>',
    "pulse": '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
}


def icon(name):
    return (f"<span class='ico'><svg viewBox='0 0 24 24' fill='none' stroke='currentColor' stroke-width='1.7' "
            f"stroke-linecap='round' stroke-linejoin='round'>{ICON[name]}</svg></span>")


def equity_svg(ch):
    """Smooth balance curve from the trades entered, with start and target lines."""
    acc = CHALLENGE["account"]
    pts = [acc]
    for x in ch["trades"]:
        pts.append(pts[-1] + x["pnl"])
    target = acc + ch["target"] if ch["target"] else None
    lo = min(pts) - 20
    hi = max(pts + ([target] if target else [acc + 60]))
    lo, hi = min(lo, acc - 30), max(hi, acc + 30)
    W, H, pad = 600, 220, 14
    n = max(len(pts) - 1, 1)
    X = lambda i: pad + (W - 2 * pad) * (i / n)
    Y = lambda v: pad + (H - 2 * pad) * (1 - (v - lo) / (hi - lo))
    xy = [(X(i), Y(v)) for i, v in enumerate(pts)] if len(pts) > 1 else [(X(0), Y(acc)), (X(1), Y(acc))]
    d = f"M{xy[0][0]:.1f},{xy[0][1]:.1f}"
    for i in range(1, len(xy)):          # Catmull-Rom → Bezier for a soft line
        p0 = xy[max(i - 2, 0)]; p1 = xy[i - 1]; p2 = xy[i]; p3 = xy[min(i + 1, len(xy) - 1)]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    area = d + f" L{xy[-1][0]:.1f},{H - pad} L{xy[0][0]:.1f},{H - pad} Z"
    guides = f"<line x1='{pad}' x2='{W - pad}' y1='{Y(acc):.1f}' y2='{Y(acc):.1f}' class='g'/>"
    labels = (f"<span class='cl' style='left:0;top:calc({100 * Y(acc) / H:.1f}% + 4px)'>start {money(acc)}</span>")
    if target:
        guides += f"<line x1='{pad}' x2='{W - pad}' y1='{Y(target):.1f}' y2='{Y(target):.1f}' class='g dash'/>"
        labels += (f"<span class='cl' style='right:0;top:calc({100 * Y(target) / H:.1f}% - 20px)'>"
                   f"target {money(target)}</span>")
    dots = "".join(f"<line x1='{x:.1f}' y1='{y:.1f}' x2='{x:.1f}' y2='{y:.1f}' class='dot'/>"
                   for x, y in xy[1:]) if len(pts) > 1 else ""
    return (f"<div class='cw'>{labels}<svg class='chart' viewBox='0 0 {W} {H}' preserveAspectRatio='none' role='img' "
            f"aria-label='Balance after each trade'><defs><linearGradient id='fa' x1='0' y1='0' x2='0' y2='1'>"
            f"<stop offset='0' class='fa0'/><stop offset='1' class='fa1'/></linearGradient></defs>"
            f"{guides}<path d='{area}' fill='url(#fa)'/><path d='{d}' class='ln'/>{dots}</svg></div>")


def ring_svg(frac, big, small):
    frac = max(0.0, min(1.0, frac))
    r, c = 52, 2 * 3.14159 * 52
    return (f"<div class='ring'><svg viewBox='0 0 140 140'><circle cx='70' cy='70' r='{r}' class='rt'/>"
            f"<circle cx='70' cy='70' r='{r}' class='rf' stroke-dasharray='{c * frac:.1f} {c:.1f}' "
            f"transform='rotate(-90 70 70)'/></svg><div class='rc'><b>{big}</b><span>{small}</span></div></div>")


CSS = """
:root{--ink:#0c0d0f;--ink2:rgba(12,13,15,.62);--ink3:rgba(12,13,15,.38);--page:#ffffff;
  --glass:rgba(255,255,255,.72);--glass2:rgba(12,13,15,.035);--edge:rgba(12,13,15,.09);
  --line:rgba(12,13,15,.08);--shadow:0 14px 40px rgba(0,0,0,.08),0 1px 3px rgba(0,0,0,.06);
  --hi:inset 0 1px 0 rgba(255,255,255,.85);--fill:rgba(12,13,15,.85);--track:rgba(12,13,15,.10);
  --solid:#0c0d0f;--solidink:#fff}
@media (prefers-color-scheme:dark){:root{--ink:#f4f5f7;--ink2:rgba(244,245,247,.66);--ink3:rgba(244,245,247,.4);--page:#000000;
  --glass:rgba(255,255,255,.06);--glass2:rgba(255,255,255,.07);--edge:rgba(255,255,255,.13);
  --line:rgba(255,255,255,.09);--shadow:0 20px 60px rgba(0,0,0,.55),0 2px 6px rgba(0,0,0,.4);
  --hi:inset 0 1px 0 rgba(255,255,255,.14);--fill:rgba(255,255,255,.92);--track:rgba(255,255,255,.12);
  --solid:#f4f5f7;--solidink:#0c0d0f}}
*{box-sizing:border-box}
html,body{background:var(--page)}
.bg{position:fixed;inset:-30px;z-index:-1;background-position:center;background-size:cover;background-repeat:no-repeat}
body.photo{background:transparent;--glass:rgba(255,255,255,.30);--glass2:rgba(255,255,255,.36);--edge:rgba(255,255,255,.6);
  --line:rgba(12,13,15,.09);--shadow:0 20px 50px rgba(0,0,0,.2),0 2px 6px rgba(0,0,0,.1)}
body.photo .head h1,body.photo .head .sub{text-shadow:0 1px 16px rgba(255,255,255,.7)}
@media (prefers-color-scheme:dark){body.photo{--glass:rgba(20,22,26,.38);--glass2:rgba(255,255,255,.08);
  --edge:rgba(255,255,255,.18);--line:rgba(255,255,255,.1)}
  body.photo .bg{filter:brightness(.62)}
  body.photo .head h1,body.photo .head .sub{text-shadow:0 1px 16px rgba(0,0,0,.7)}}
.bgctl{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px;align-items:center}
.bgctl label{cursor:pointer}
body{margin:0;color:var(--ink);min-height:100vh;overflow-x:hidden;
  font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased}
main{max-width:1180px;margin:0 auto;padding:16px 16px 120px}
.glass{background:var(--glass);border:1px solid var(--edge);border-radius:24px;box-shadow:var(--hi),var(--shadow);
  -webkit-backdrop-filter:blur(26px) saturate(150%);backdrop-filter:blur(26px) saturate(150%)}
.top{display:flex;align-items:center;gap:12px;padding:8px 10px 8px 8px;border-radius:99px;margin-bottom:22px}
.avatar{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-weight:700;font-size:13px;
  background:var(--solid);color:var(--solidink);flex:none}
.top .who{flex:1;min-width:0;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.top .who span{color:var(--ink2);font-weight:500}
.status{padding:6px 12px;border-radius:99px;font-size:12px;font-weight:700;background:var(--glass2);border:1px solid var(--edge);white-space:nowrap}
.status.bad{background:var(--solid);color:var(--solidink);border-color:transparent}
.head{display:flex;justify-content:space-between;align-items:flex-end;gap:12px;flex-wrap:wrap;margin:0 4px 16px}
h1{font-size:34px;letter-spacing:-.03em;margin:0;line-height:1.1}
.sub{color:var(--ink2);font-size:13px}
.seg{display:flex;gap:4px;padding:4px;border-radius:99px}
.seg span{padding:6px 12px;border-radius:99px;font-size:12px;font-weight:600;color:var(--ink2)}
.seg span.on{background:var(--solid);color:var(--solidink)}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.kpi{padding:16px;border-radius:22px;min-width:0}
.kpi .lab{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--ink2);font-weight:500}
.ico{width:30px;height:30px;border-radius:50%;display:grid;place-items:center;background:var(--glass2);
  border:1px solid var(--edge);color:var(--ink);flex:none}.ico svg{width:16px;height:16px}
.kpi b{display:block;font-size:30px;letter-spacing:-.03em;margin:12px 0 6px;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;font-weight:700;background:var(--solid);color:var(--solidink)}
.chip.soft{background:var(--glass2);color:var(--ink);border:1px solid var(--edge)}
.kpi .note{display:block;font-size:12px;color:var(--ink2);margin-top:7px}
.bar{height:6px;background:var(--track);border-radius:99px;overflow:hidden;margin-top:10px}
.bar i{display:block;height:100%;background:var(--fill);border-radius:99px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-top:14px}
.grid.even{grid-template-columns:1fr 1fr}
.card{padding:18px;min-width:0}
.card h2{font-size:16px;margin:0;letter-spacing:-.01em}
.card .sub{margin-bottom:12px}
.chart{width:100%;height:220px;display:block;overflow:visible}
.chart .ln{fill:none;stroke:var(--ink);stroke-width:2.4;vector-effect:non-scaling-stroke}
.chart .dot{stroke:var(--ink);stroke-width:7;stroke-linecap:round;vector-effect:non-scaling-stroke}
.chart .g{stroke:var(--ink3);stroke-width:1;vector-effect:non-scaling-stroke}.chart .dash{stroke-dasharray:5 5}
.cw{position:relative}.cl{position:absolute;font-size:11.5px;color:var(--ink2);padding:1px 7px;border-radius:99px;background:var(--glass2);border:1px solid var(--edge);white-space:nowrap;z-index:1}
.fa0{stop-color:var(--ink);stop-opacity:.16}.fa1{stop-color:var(--ink);stop-opacity:0}
.ring{position:relative;width:180px;height:180px;margin:6px auto 10px}.ring svg{width:100%;height:100%}
.ring .rt{fill:none;stroke:var(--track);stroke-width:14}
.ring .rf{fill:none;stroke:var(--fill);stroke-width:14;stroke-linecap:round}
.rc{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.rc b{font-size:26px;letter-spacing:-.02em}.rc span{font-size:12px;color:var(--ink2)}
.legend div{display:flex;justify-content:space-between;font-size:13px;padding:6px 0;border-top:1px solid var(--line)}
.legend div:first-child{border-top:0}.legend span{color:var(--ink2)}
.pairs{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.pair{padding:14px;border-radius:18px;background:var(--glass2);border:1px solid var(--edge);box-shadow:var(--hi)}
.pair h3{margin:0 0 2px;font-size:16px;display:flex;justify-content:space-between;align-items:center;gap:6px}
.pair .tagb{white-space:nowrap;font-size:11px;font-weight:700;padding:2px 8px;border-radius:99px;background:var(--solid);color:var(--solidink)}
.pair .tagb.soft{background:transparent;color:var(--ink2);border:1px solid var(--line)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:3px 10px;font-size:13px;margin-top:8px}
.kv dt{color:var(--ink2)}.kv dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
.big{font-size:22px;font-weight:700;letter-spacing:-.02em;margin-top:6px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:9px 4px;border-top:1px solid var(--line)}th{color:var(--ink2);font-weight:500;font-size:12px;border-top:0}
td.num{text-align:right;font-variant-numeric:tabular-nums}td.neg{color:var(--ink2)}
.ev{display:flex;gap:10px;align-items:flex-start;font-size:14px;padding:10px 0;border-top:1px solid var(--line)}
.ev:first-of-type{border-top:0}.ev .tg{font-size:10.5px;font-weight:800;letter-spacing:.06em;padding:3px 7px;border-radius:7px;
  background:var(--glass2);border:1px solid var(--edge);white-space:nowrap}.ev .tg.solid{background:var(--solid);color:var(--solidink);border-color:transparent}
.ev .t{color:var(--ink2);font-size:12px}
form{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin:12px 0}
input,select,button{font:inherit;padding:11px 13px;border-radius:14px;min-width:0;width:100%;color:var(--ink);outline:none;
  background:var(--glass2);border:1px solid var(--edge);box-shadow:var(--hi)}
input::placeholder{color:var(--ink3)}input:focus,select:focus{border-color:var(--ink3)}
button{background:var(--solid);color:var(--solidink);border:0;font-weight:700;cursor:pointer}
button.ghost{background:var(--glass2);color:var(--ink);border:1px solid var(--edge);width:auto;padding:5px 11px;font-size:12px;font-weight:600}
.msg{font-size:13px}
.done{padding:10px 12px;border-radius:14px;background:var(--solid);color:var(--solidink);font-weight:700;margin:10px 0}
.hgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.hgrid>div{padding:12px 14px;border-radius:16px;background:var(--glass2);border:1px solid var(--edge)}
.hgrid b{display:block;font-size:20px;letter-spacing:-.02em}.hgrid span{font-size:12px;color:var(--ink2)}
.warnline{text-decoration:underline;text-decoration-thickness:2px;text-underline-offset:3px}
.nav{position:fixed;left:50%;bottom:max(14px,env(safe-area-inset-bottom));transform:translateX(-50%);display:flex;gap:2px;
  padding:6px;border-radius:99px;z-index:5;max-width:calc(100vw - 24px);overflow-x:auto;scrollbar-width:none}
.nav::-webkit-scrollbar{display:none}
.nav a{padding:9px 14px;border-radius:99px;font-size:13px;font-weight:600;color:var(--ink2);text-decoration:none;white-space:nowrap}
.nav a.on{background:var(--solid);color:var(--solidink)}
section{scroll-margin-top:16px}
@media (max-width:900px){.kpis,.pairs,.hgrid{grid-template-columns:1fr 1fr}.grid,.grid.even{grid-template-columns:1fr}
  h1{font-size:30px}.kpi b{font-size:24px}}
@media (max-width:600px){.pairs{grid-template-columns:1fr}}
@media (max-width:380px){.kpi b{font-size:21px}.kpi{padding:13px}}
"""


def render(now=None):
    now = now or datetime.now(timezone.utc)
    st = read_json(P("status.json"), {})
    h = health(now)
    ch = challenge_view(now)
    recent, all_ev = events()
    acc = CHALLENGE["account"]
    o = []

    status = ("<span class='status'>● Running</span>" if h["ok"] else
              "<span class='status bad'>⚠︎ Check the bot</span>")
    o.append(f"<div class='top glass'><div class='avatar'>FB</div><div class='who'>Fib Bot "
             f"<span>· {esc(ago(h['last'], now))}</span></div>{status}</div>")
    phase = "Funded" if ch["funded"] else f"Phase {ch['phase']}"
    o.append(f"<div class='head' id='overview'><div><h1>Overview</h1><div class='sub'>FundedNext 2-Step · "
             f"{money(acc)} · updated {esc(lagos(now, '%H:%M'))} Lagos</div></div>"
             f"<div class='seg glass'>" + "".join(
                 f"<span class='{'on' if (p == phase) else ''}'>{p}</span>" for p in ("Phase 1", "Phase 2", "Funded"))
             + "</div></div>")

    # KPI cards
    left = ch["target"] - ch["profit"]
    pct = max(0, min(100, 100 * ch["profit"] / ch["target"])) if ch["target"] else 0
    own = CHALLENGE["own_daily_stop"]
    used_pct = min(100, 100 * ch["used"] / ch["daily_limit"])
    stop_chip = ("<span class='chip'>stop for today</span>" if ch["used"] >= own else
                 f"<span class='chip soft'>{money(own - ch['used'])} to your stop</span>")
    tgt_chip = ("<span class='chip'>reached</span>" if (not ch["funded"] and left <= 0)
                else f"<span class='chip soft'>{money(max(left, 0))} to go</span>" if not ch["funded"] else "")
    o.append("<div class='kpis'>"
             f"<div class='kpi glass'><div class='lab'>{icon('wallet')}Balance</div><b>{money(ch['balance'])}</b>"
             f"<span class='chip soft'>{money(ch['profit'], True)}</span><span class='note'>this phase</span></div>"
             f"<div class='kpi glass'><div class='lab'>{icon('target')}Profit target</div>"
             f"<b>{pct:.0f}%</b>{tgt_chip}<span class='note'>of {money(ch['target'])}</span>"
             f"<div class='bar'><i style='width:{pct:.0f}%'></i></div></div>"
             f"<div class='kpi glass'><div class='lab'>{icon('shield')}Lost today</div>"
             f"<b class='{'warnline' if ch['used'] >= own else ''}'>{money(ch['used'])}</b>{stop_chip}"
             f"<span class='note'>limit {money(ch['daily_limit'])}</span>"
             f"<div class='bar'><i style='width:{used_pct:.0f}%'></i></div></div>"
             f"<div class='kpi glass'><div class='lab'>{icon('cal')}Trading days</div>"
             f"<b>{ch['days']} / {CHALLENGE['min_days']}</b>"
             f"<span class='chip {'' if ch['days'] >= CHALLENGE['min_days'] else 'soft'}'>"
             f"{'done' if ch['days'] >= CHALLENGE['min_days'] else str(CHALLENGE['min_days'] - ch['days']) + ' more'}</span>"
             f"<span class='note'>resets {esc(lagos(ch['reset'], '%H:%M'))}</span></div></div>")

    # chart + ring
    ring = ring_svg((ch["profit"] / ch["target"]) if ch["target"] else 0,
                    money(max(ch["profit"], 0)), f"of {money(ch['target'])} target" if ch["target"] else "profit")
    o.append("<div class='grid'>"
             f"<div class='card glass'><h2>Balance after each trade</h2>"
             f"<div class='sub'>{len(ch['trades'])} trade{'' if len(ch['trades']) == 1 else 's'} entered · "
             f"{ch['wins']} win{'' if ch['wins'] == 1 else 's'}, {ch['losses']} loss{'' if ch['losses'] == 1 else 'es'}</div>"
             f"{equity_svg(ch)}</div>"
             f"<div class='card glass'><h2>Progress</h2><div class='sub'>Toward this phase's target</div>{ring}"
             f"<div class='legend'><div><span>Room above floor</span><b>{money(ch['room'])}</b></div>"
             f"<div><span>Floor</span><b>{money(ch['floor'])}</b></div>"
             f"<div><span>Today</span><b>{money(ch['today_pnl'], True)}</b></div></div></div></div>")

    # pairs
    stp = {p["sym"]: p for p in st.get("pairs", [])}
    cards = []
    for sym in PAIRS:
        p = stp.get(sym, {})
        bias = p.get("bias")
        line = p.get("line", "no data yet").split(": ", 1)[-1]
        tag = ""
        if p.get("in_zone"):
            tag = "<span class='tagb'>IN ZONE</span>"
        elif bias:
            tag = f"<span class='tagb soft'>{'▲ bullish' if bias == 'bullish' else '▼ bearish'}</span>"
        price = p.get("price")
        z = p.get("zone")
        body = f"<div class='big'>{fmt(sym, price)}</div>" if price else ""
        if z:
            need = p.get("need")
            side = "Sell" if bias == "bearish" else "Buy"
            reach = (price >= need) if bias == "bearish" else (price <= need)
            body += (f"<dl class='kv'><dt>Zone</dt><dd>{fmt(sym, z['z_bot'])}–{fmt(sym, z['z_top'])}</dd>"
                     f"<dt>{side} needs</dt><dd>{fmt(sym, need)}</dd>"
                     f"<dt>Distance</dt><dd>{'reached' if reach else pips(sym, need - price)}</dd>"
                     f"<dt>SL / TP1</dt><dd>{fmt(sym, z['sl'])} / {fmt(sym, z['tp1'])}</dd></dl>")
        cards.append(f"<div class='pair'><h3>{esc(sym)}{tag}</h3><div class='sub'>{esc(line)}</div>{body}</div>")
    st_t = parse_t(st.get("t", ""))
    o.append(f"<section id='pairs' class='card glass' style='margin-top:14px'><h2>Pairs</h2>"
             f"<div class='sub'>Live read as of {esc(lagos(st_t, '%H:%M'))} Lagos</div>"
             f"<div class='pairs'>{''.join(cards)}</div></section>")

    # alerts + tracked signals
    ev = []
    for e in recent:
        t = parse_t(e.get("t", ""))
        sym = e.get("sym", "")
        if e["kind"] == "armed":
            tg, txt = "<span class='tg'>ZONE</span>", (f"<b>{esc(sym)}</b> entered its zone · "
                       f"{'sell' if e.get('dir') == 'bearish' else 'buy'} needs {fmt(sym, e.get('need'))}")
        elif e["kind"] == "entry":
            tg = f"<span class='tg solid'>{'▼ SELL' if e.get('dir') == 'bearish' else '▲ BUY'}</span>"
            txt = (f"<b>{esc(sym)}</b> at {fmt(sym, e.get('price'))} · SL {fmt(sym, e.get('sl'))} · "
                   f"TP1 {fmt(sym, e.get('tp1'))} · {esc(e.get('lots'))} lots")
        elif e["kind"] == "closed":
            r = e.get("R") or 0
            tg = f"<span class='tg {'solid' if r > 0 else ''}'>{'✓ WIN' if r > 0 else '✕ LOSS'}</span>"
            txt = f"<b>{esc(sym)}</b> {esc(e.get('result'))} · {r:+.2f}R"
        else:
            continue
        ev.append(f"<div class='ev'>{tg}<div>{txt}<div class='t'>{esc(lagos(t))}</div></div></div>")
    opn = "".join(
        f"<tr><td>{esc(t['sym'])}</td><td>{'Buy' if t['dir'] == 'bullish' else 'Sell'}</td>"
        f"<td class='num'>{fmt(t['sym'], t['entry'])}</td><td class='num'>{fmt(t['sym'], t['sl'])}</td>"
        f"<td class='num'>{fmt(t['sym'], t['tp1'])}{' ✓' if t.get('hit_tp1') else ''}</td></tr>" for t in h["open"])
    closed = [e for e in all_ev if e.get("kind") == "closed"]
    o.append("<div class='grid even'>"
             f"<section id='alerts' class='card glass'><h2>Recent alerts</h2><div class='sub'>"
             f"{len(closed)} signal{'' if len(closed) == 1 else 's'} closed · "
             f"{sum(e.get('R') or 0 for e in closed):+.2f}R total</div>"
             f"{''.join(ev) or '<div class=sub>No alerts recorded yet.</div>'}</section>"
             f"<div class='card glass'><h2>Signals being tracked</h2><div class='sub'>Followed to TP / SL automatically</div>"
             + (f"<table><tr><th>Pair</th><th>Side</th><th class='num'>Entry</th><th class='num'>SL</th>"
                f"<th class='num'>TP1</th></tr>{opn}</table>" if opn else "<div class='sub'>Nothing open right now.</div>")
             + "</div></div>")

    # trades / tracker
    o.append(f"<section id='trades' class='card glass' style='margin-top:14px'><h2>Your trades</h2>"
             f"<div class='sub'>Enter each closed trade from MT5 · {esc(phase)}</div>"
             + ("<div class='done'>✓ Target and trading days met — check your FundedNext dashboard.</div>"
                if (not ch["funded"] and left <= 0 and ch["days"] >= CHALLENGE["min_days"]) else "")
             + _trade_form(ch) + "</section>")

    # health
    age = h["age"]
    o.append("<section id='health' class='card glass' style='margin-top:14px'><h2>Bot health</h2>"
             f"<div class='sub'>Last heartbeat {esc(h['heartbeat'] or 'none yet')} · risk {esc(st.get('risk', '—'))}% "
             f"per trade · min reward:risk {esc(st.get('min_rr', '—'))}</div><div class='hgrid'>"
             f"<div><b class='{'' if age is not None and age <= 3 else 'warnline'}'>{esc(ago(h['last'], now))}</b><span>last check</span></div>"
             f"<div><b class='{'' if h['runs'] >= 50 else 'warnline'}'>{h['runs']} / 60</b><span>checks last hour</span></div>"
             f"<div><b>{h['hiccups']}</b><span>OANDA hiccups (auto-retried)</span></div>"
             f"<div><b class='{'' if h['tg_err'] == 0 else 'warnline'}'>{'OK' if h['tg_err'] == 0 else h['tg_err']}</b>"
             f"<span>Telegram</span></div></div>"
             "<div class='bgctl'><label class='status'>Change background"
             "<input type='file' accept='image/*' id='bgfile' hidden onchange='setBg(this)'></label>"
             + ("<button class='ghost' onclick=\"act({action:'bg_remove'},'Go back to the silver background?')\">"
                "Back to silver background</button>" if os.path.exists(BG_FILE) else "")
             + "<button class='ghost' onclick=\"logout()\">Log out</button>"
             + "<span id='bgmsg' class='sub'></span></div></section>")

    o.append("<nav class='nav glass'><a href='#overview' class='on'>Overview</a><a href='#pairs'>Pairs</a>"
             "<a href='#alerts'>Alerts</a><a href='#trades'>Trades</a><a href='#health'>Health</a></nav>")
    if os.path.exists(BG_FILE):          # your own picture
        v = int(os.path.getmtime(BG_FILE))
        url = f"bg?v={v}"
    else:                                # default: the soft silver room
        url = "bg-default.jpg"
    bodyclass, bgdiv = " class='photo'", f"<div class='bg' style=\"background-image:url('{url}')\"></div>"
    return (PAGE.replace("{{CSS}}", CSS).replace("{{BODYCLASS}}", bodyclass)
            .replace("{{BGDIV}}", bgdiv).replace("{{BODY}}", "".join(o)))


def _trade_form(ch):
    nowl = datetime.now(LAGOS).strftime("%Y-%m-%dT%H:%M")
    rows = "".join(
        f"<tr><td>{esc(lagos(parse_t(x['t']), '%d %b %H:%M'))}</td><td>{esc(x['pair'])} {esc(x.get('side', ''))}"
        f"{'<div class=sub>' + esc(x['note']) + '</div>' if x.get('note') else ''}</td>"
        f"<td class='num {'' if x['pnl'] > 0 else 'neg'}'><b>{money(x['pnl'], True)}</b></td>"
        f"<td class='num'><button class='ghost' aria-label='Remove trade' "
        f"onclick=\"act({{action:'delete',id:'{esc(x['id'])}'}},'Remove this trade?')\">✕</button></td></tr>"
        for x in reversed(ch["trades"]))
    nxt = {1: (2, "I passed Phase 1 → track Phase 2"), 2: (3, "I'm funded → track funded account"),
           3: (1, "Start a new challenge")}[ch["phase"]]
    return (
        "<form id='tf' onsubmit='addTrade(event)'>"
        f"<select name='pair' aria-label='Pair'>{''.join(f'<option>{p}</option>' for p in PAIRS + ['Other'])}</select>"
        "<select name='side' aria-label='Side'><option>Sell</option><option>Buy</option></select>"
        "<input name='pnl' inputmode='decimal' placeholder='P/L $, e.g. -29.80' required aria-label='Profit or loss'>"
        f"<input name='closed' type='datetime-local' value='{nowl}' aria-label='Closed at'>"
        "<input name='note' maxlength='120' placeholder='Note (optional)' aria-label='Note'>"
        "<button>Add trade</button></form><div id='msg' class='msg'></div>"
        + ((f"<table><tr><th>Closed (Lagos)</th><th>Trade</th><th class='num'>P/L</th><th></th></tr>{rows}</table>")
           if rows else "<div class='sub'>No trades entered yet.</div>")
        + f"<div style='margin-top:12px'><button class='ghost' onclick=\"act({{action:'phase',phase:{nxt[0]}}},"
          f"'This archives the current trades. Continue?')\">{esc(nxt[1])}</button></div>")


def login_page(msg="", form=True):
    """Password screen, same glass look over the silver background."""
    body = ("<div class='login glass'><div class='avatar'>FB</div><h1>Fib Bot</h1>"
            "<div class='sub'>Enter your dashboard password</div>")
    if form:
        body += ("<form id='lf' onsubmit='login(event)'>"
                 "<input name='password' type='password' autocomplete='current-password' "
                 "placeholder='Password' required autofocus aria-label='Password'>"
                 "<button>Unlock</button></form>")
    body += f"<div id='lmsg' class='sub' style='min-height:18px'>{esc(msg)}</div></div>"
    js = """<script>
async function login(e){
  e.preventDefault();
  const m = document.getElementById('lmsg'); m.textContent = 'Checking…';
  const r = await fetch('login', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({password: e.target.password.value})});
  const j = await r.json().catch(()=>({ok:false,msg:'Server error'}));
  if(j.ok) location.reload(); else { m.textContent = j.msg; e.target.password.value=''; }
}
</script>"""
    css = CSS + """
.login{max-width:360px;margin:18vh auto 0;padding:28px 24px;text-align:center}
.login .avatar{margin:0 auto 14px;width:52px;height:52px;font-size:17px}
.login h1{font-size:28px;margin:0 0 4px}.login form{grid-template-columns:1fr;margin:18px 0 8px}"""
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex,nofollow"><title>Fib Bot</title>'
            f"<style>{css}</style></head><body class='photo'>"
            "<div class='bg' style=\"background-image:url('bg-default.jpg')\"></div>"
            f"<main>{body}</main>{js}</body></html>")


PAGE = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default"><meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)"><meta name="theme-color" content="#000000" media="(prefers-color-scheme: dark)"><title>Fib Bot</title>
<style>{{CSS}}</style></head><body{{BODYCLASS}}>{{BGDIV}}<main>{{BODY}}</main>
<script>
async function post(body){
  const r = await fetch('api', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
  if(r.status === 401){ location.reload(); return {ok:false}; }
  return r.json().catch(()=>({ok:false,msg:'Server error'}));
}
async function logout(){
  if(!confirm('Log out on this device?')) return;
  await fetch('logout', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
  location.reload();
}
async function act(body, ask){
  if(ask && !confirm(ask)) return;
  const j = await post(body);
  if(j.ok) location.reload(); else alert(j.msg || 'Failed');
}
async function addTrade(e){
  e.preventDefault();
  const f = new FormData(e.target), body = {action:'add'};
  f.forEach((v,k)=>body[k]=v);
  body.pnl = String(body.pnl).replace(/[$,\\s]/g,'').replace('\\u2212','-');
  const j = await post(body);
  if(j.ok) location.reload(); else document.getElementById('msg').textContent = j.msg;
}
async function setBg(input){
  const f = input.files && input.files[0]; if(!f) return;
  const m = document.getElementById('bgmsg'); m.textContent = 'Preparing photo…';
  try{
    const img = await createImageBitmap(f);
    const k = Math.min(1, 2560 / Math.max(img.width, img.height));
    const c = document.createElement('canvas');
    c.width = Math.round(img.width * k); c.height = Math.round(img.height * k);
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    const blob = await new Promise(r => c.toBlob(r, 'image/jpeg', 0.84));
    m.textContent = 'Uploading…';
    const r = await fetch('bg', {method:'POST', headers:{'Content-Type':'image/jpeg'}, body: blob});
    const j = await r.json().catch(()=>({ok:false,msg:'Upload failed'}));
    if(j.ok) location.reload(); else m.textContent = j.msg || 'Upload failed';
  }catch(e){ m.textContent = 'Could not read that picture — try a JPG or PNG.'; }
}
const links = [...document.querySelectorAll('.nav a')];
const secs = links.map(a => document.querySelector(a.getAttribute('href')));
addEventListener('scroll', () => {
  let i = 0; secs.forEach((s, k) => { if (s && s.getBoundingClientRect().top < innerHeight * 0.4) i = k; });
  links.forEach((a, k) => a.classList.toggle('on', k === i));
}, {passive:true});
setInterval(()=>{
  const a = document.activeElement, f = document.getElementById('tf');
  const typing = a && ['INPUT','SELECT'].includes(a.tagName);
  const filled = f && (f.pnl.value || f.note.value);
  if(!typing && !filled) location.reload();
}, 60000);
</script></body></html>"""


if __name__ == "__main__":
    print(render())
