#!/usr/bin/env bash
# Sets up the private dashboard web page on the VPS.  Run once:
#   bash forex-bot/dashboard_setup.sh
# Safe to run again (keeps the same link).  New link:  bash forex-bot/dashboard_setup.sh --new-link
set -e
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"
PORT=8080

echo "== 1/4  Secret link =="
if [ "$1" = "--new-link" ] || [ ! -s .dashboard_token ]; then
  ./venv/bin/python -c "import secrets; print(secrets.token_urlsafe(18))" > .dashboard_token
  echo "  new private link created"
else
  echo "  keeping your existing link"
fi
chmod 600 .dashboard_token

echo "== 2/4  Background service (starts on boot, restarts if it stops) =="
cat > /etc/systemd/system/fibdash.service <<EOF
[Unit]
Description=Fib bot dashboard
After=network-online.target

[Service]
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
