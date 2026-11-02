#!/usr/bin/env bash
# Sets up the private dashboard web page on the VPS.  Run once:
#   bash forex-bot/dashboard_setup.sh
# Safe to run again (keeps the same link and password).
# New link:      bash forex-bot/dashboard_setup.sh --new-link
# New password:  bash forex-bot/dashboard_setup.sh --password
set -e
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

echo "== 1/4  Secret link =="
if [ "$1" = "--new-link" ] || [ ! -s .dashboard_token ]; then
  ./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(18))" > .dashboard_token
  echo "  new private link created"
else
  echo "  keeping your existing link"
fi
chmod 600 .dashboard_token

echo "== Password (asked once on each phone / Mac) =="
if [ "$1" = "--password" ] || [ ! -s .dashboard_pass ]; then
  while true; do
    read -r -s -p "  Choose a dashboard password (8+ characters): " P1; echo
    read -r -s -p "  Type it again: " P2; echo
    if [ "$P1" != "$P2" ]; then echo "  They don't match — try again."; continue; fi
    if [ ${#P1} -lt 8 ]; then echo "  Too short — use at least 8 characters."; continue; fi
    break
  done
  printf '%s' "$P1" | ./venv/bin/python -c '
import sys, os, hashlib
pw = sys.stdin.read().encode(); salt = os.urandom(16); it = 300000
print("pbkdf2_sha256$%d$%s$%s" % (it, salt.hex(), hashlib.pbkdf2_hmac("sha256", pw, salt, it).hex()))
' > .dashboard_pass
  unset P1 P2
  chmod 600 .dashboard_pass
  rm -f .dashboard_sessions.json      # new password logs every device out
  echo "  ✓ password saved"
else
  echo "  keeping your existing password (change it with: --password)"
fi

echo "== 2/4  Background service (starts on boot, restarts if it stops) =="
# pick a free port (8080 is often taken by other software)
systemctl stop fibdash >/dev/null 2>&1 || true
sleep 1
PORT=""
for p in $(cat .dashboard_port 2>/dev/null) 8080 8787 8888 9090 9443 18080; do
  if ! ss -ltnH "sport = :$p" | grep -q .; then PORT=$p; break; fi
done
[ -z "$PORT" ] && { echo "  ❌ no free port found"; exit 1; }
echo "$PORT" > .dashboard_port
echo "  using port $PORT"
cat > /etc/systemd/system/fibdash.service <<EOF
[Unit]
Description=Fib bot dashboard
After=network-online.target

[Service]
Environment=DASH_PORT=$PORT
WorkingDirectory=$BOT_DIR
ExecStart=$BOT_DIR/venv/bin/python $BOT_DIR/dash_server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fibdash >/dev/null 2>&1
systemctl restart fibdash

echo "== 3/4  Firewall: allow the dashboard port =="
ufw allow $PORT/tcp >/dev/null

echo "== 4/4  Checking it works =="
IP=$(curl -s --max-time 5 -4 https://ifconfig.me || hostname -I | awk '{print $1}')
URL="http://$IP:$PORT/$(cat .dashboard_token)/"
code=000
for i in $(seq 1 15); do            # give it up to 15 seconds to start
  code=$(curl -s --max-time 3 -o /dev/null -w "%{http_code}" "http://127.0.0.1:$PORT/$(cat .dashboard_token)/" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
if [ "$code" = "200" ]; then
  echo "  ✅ dashboard is running"
else
  echo "  ❌ dashboard did not start (answer: $code). Last messages from it:"
  journalctl -u fibdash -n 15 --no-pager | sed 's/^/     /'
  exit 1
fi

# send the link privately to your Telegram so you can tap it
set -a; . ./.env; set +a
curl -s --max-time 10 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d chat_id="$TELEGRAM_CHAT_ID" -d disable_web_page_preview=true \
  --data-urlencode text="📊 Your Fib Bot dashboard (keep this link private):
$URL" >/dev/null && echo "  ✅ link sent to your Telegram"

echo
echo "========================================================"
echo " Dashboard link (also sent to Telegram — keep it private):"
echo "   $URL"
echo " Tip: open it in Safari, then Share → Add to Home Screen."
echo "========================================================"
