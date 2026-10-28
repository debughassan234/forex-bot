#!/usr/bin/env bash
# Daily health check for the Fib bot.
#   bash forex-bot/check.sh          full check (no Telegram message sent)
#   bash forex-bot/check.sh --send   also sends a test message to Telegram
cd "$(dirname "$0")" || exit 1
LOG=logs/bot.log
ok()  { echo "  ✅ $*"; }
bad() { echo "  ❌ $*"; }

echo "================ FIB BOT HEALTH CHECK ================"
echo "  Now: $(date -u '+%Y-%m-%d %H:%M') UTC  ($(TZ=Africa/Lagos date '+%H:%M') Lagos)"
echo

echo "1. Schedule (every minute)"
if crontab -l 2>/dev/null | grep -q "run_bot.sh"; then ok "saved: $(crontab -l | grep run_bot.sh)"
else bad "NOT saved — run: bash forex-bot/setup_vps.sh"; fi
if systemctl is-active --quiet cron; then ok "scheduler is running"
else bad "scheduler stopped — run: systemctl start cron"; fi
echo

echo "2. Is it checking every minute?"
if [ ! -f "$LOG" ]; then
  bad "no log yet"
else
  last=$(grep -E '^=== ' "$LOG" | tail -1 | sed -E 's/=== (.*) UTC ===/\1/')
  if [ -n "$last" ]; then
    age=$(( ( $(date -u +%s) - $(date -u -d "$last" +%s) ) / 60 ))
    since=$(date -u -d '10 min ago' '+%Y-%m-%d %H:%M:%S')
    runs=$(grep -E '^=== ' "$LOG" | sed -E 's/=== (.*) UTC ===/\1/' | awk -v s="$since" '$0 >= s' | wc -l)
    if [ "$age" -le 2 ]; then ok "last check ${age} min ago ($last UTC)"
    else bad "last check ${age} min ago — it should be 0-1. Check part 1."; fi
    if [ "$runs" -ge 8 ]; then ok "$runs checks in the last 10 minutes"
    else bad "only $runs checks in the last 10 minutes (expected ~10)"; fi
  fi
fi
echo

echo "3. Telegram"
set -a; . ./.env; set +a
me=$(curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe")
if echo "$me" | grep -q '"ok":true'; then
  ok "bot token works (@$(echo "$me" | sed -E 's/.*"username":"([^"]*)".*/\1/'))"
else bad "Telegram rejected the token: $(echo "$me" | sed -E 's/.*"description":"([^"]*)".*/\1/')"; fi
if [ "$1" = "--send" ]; then
  r=$(curl -s --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" -d text="✅ Health check from your server — Telegram works")
  if echo "$r" | grep -q '"ok":true'; then ok "test message sent — check Telegram"
  else bad "message failed: $(echo "$r" | sed -E 's/.*"description":"([^"]*)".*/\1/')"; fi
fi
tg_warn=$(tail -n 2000 "$LOG" 2>/dev/null | grep -c "telegram")
if [ "$tg_warn" -eq 0 ]; then ok "no Telegram errors in recent runs"
else bad "$tg_warn Telegram error(s) recently:"; tail -n 2000 "$LOG" | grep "telegram" | tail -2 | sed 's/^/       /'; fi
echo

echo "4. Price data (OANDA)"
blk=$(awk '/^=== /{b=""} {b=b $0 "\n"} END{printf "%s", b}' "$LOG" 2>/dev/null)
if echo "$blk" | grep -q "data unavailable"; then bad "OANDA data missing — the OANDA token may be wrong or expired"
else ok "all pairs read normally"; fi
errs=$(tail -n 2000 "$LOG" 2>/dev/null | grep -cE "Traceback|\[error\]")
if [ "$errs" -eq 0 ]; then ok "no crashes in recent runs"
else bad "$errs error line(s) recently:"; tail -n 2000 "$LOG" | grep -E "Traceback|\[error\]" | tail -2 | sed 's/^/       /'; fi
echo

echo "5. Current read (latest check)"
echo "$blk" | sed 's/^/     /'
echo

echo "6. Signals and tracked trades"
python3 - <<'EOF'
import json
try:
    st = json.load(open("bot_state.json"))
except Exception:
    st = {}
sent = [(k, v["t"][:16].replace("T", " ")) for k, v in st.items()
        if isinstance(v, dict) and "t" in v]
print(f"  signals and zone alerts remembered: {len(sent)} (latest 5 below)")
for k, t in sorted(sent, key=lambda x: x[1])[-5:]:
    kind = "zone alert" if k.endswith("|armed") else "ENTRY"
    print(f"     {t} UTC  {k.split('|')[0]}  {kind}")
tr = st.get("open_trades", [])
print(f"  trades being tracked for TP/SL: {len(tr)}")
for t in tr:
    print(f"     {t['sym']} {t['dir']} from {t['entry']}  SL {t['sl']:.5g}  TP1 {t['tp1']:.5g}"
          + ("  (TP1 hit)" if t.get("hit_tp1") else ""))
print(f"  last heartbeat: {st.get('heartbeat_day', 'none yet')}")
EOF
echo "======================================================"
