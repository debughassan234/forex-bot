#!/usr/bin/env bash
# One-time setup for the Fib bot on a fresh Ubuntu VPS (Vultr).
# Run from inside the cloned repo:   bash setup_vps.sh
# Safe to run again later — it just refreshes everything.

set -e
BOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$BOT_DIR"

echo
echo "== 1/5  Installing Python and tools (takes 1-3 minutes) =="
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv git cron ufw unattended-upgrades >/dev/null
timedatectl set-timezone UTC 2>/dev/null || true

echo "== 2/5  Basic security: firewall (SSH only) + automatic security updates =="
ufw allow OpenSSH >/dev/null
ufw --force enable >/dev/null
dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true

echo "== 3/5  Creating the Python environment =="
python3 -m venv venv
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q requests

echo "== 4/5  Your keys (stored only on this server, in .env) =="
ask() {   # ask NAME "label"  -> hidden input, shows length so you know the paste worked
  local name="$1" label="$2" val=""
  while [ -z "$val" ]; do
    read -r -s -p "  Paste $label, then press Enter: " val
    echo
    val="$(echo "$val" | tr -d '[:space:]')"
    [ -z "$val" ] && echo "  Nothing received — try again."
  done
  echo "  ✓ got ${#val} characters"
  printf '%s=%s\n' "$name" "$val" >> .env.new
}

if [ -f .env ]; then
  read -r -p "  Keys already saved. Replace them? (y/N): " redo
else
  redo="y"
fi
if [ "$redo" = "y" ] || [ "$redo" = "Y" ]; then
  rm -f .env.new
  ask TELEGRAM_BOT_TOKEN "your Telegram bot token"
  ask TELEGRAM_CHAT_ID   "your Telegram chat ID"
  ask OANDA_API_KEY      "your OANDA API token"
  echo "OANDA_ENV=practice" >> .env.new
  mv .env.new .env
fi
chmod 600 .env
chmod +x run_bot.sh

echo "== 5/5  Scheduling: every minute, every day =="
CRON_LINE="* * * * * $BOT_DIR/run_bot.sh"
( crontab -l 2>/dev/null | grep -v "run_bot.sh" ; echo "$CRON_LINE" ) | crontab -
systemctl enable --now cron >/dev/null 2>&1 || true

echo
echo "Sending a test status message to your Telegram..."
bash run_bot.sh --report
tail -n 8 logs/bot.log
echo
echo "========================================================"
echo " Done. Check Telegram for a '🔎 Manual check' message."
echo " The bot now runs by itself every minute, 24/7."
echo
echo " Useful commands:"
echo "   bash $BOT_DIR/run_bot.sh --report    status to Telegram now"
echo "   tail -n 40 $BOT_DIR/logs/bot.log     see recent runs"
echo "   cd $BOT_DIR && git pull              get bot updates from GitHub"
echo "========================================================"
