#!/usr/bin/env bash
# Runs the bot once. Called by cron every minute on the VPS.
# Usage:  bash run_bot.sh            normal run (signals, tracking, heartbeat)
#         bash run_bot.sh --report   send a status read to Telegram only

cd "$(dirname "$0")" || exit 1

# keys live in .env on the server only (never in GitHub)
set -a
. ./.env
set +a

mkdir -p logs

# never let two runs overlap; keep the log from growing forever
exec 9>/tmp/fib_bot.lock
if [ "$1" = "--report" ]; then
  flock -w 90 9 || exit 1     # a status check waits for a running check to finish
else
  flock -n 9 || exit 0        # a scheduled run skips if the previous one is still going
fi

{
  echo "=== $(date -u '+%Y-%m-%d %H:%M:%S') UTC ==="
  ./venv/bin/python fib_structure_bot.py "$@"
} >> logs/bot.log 2>&1

tail -n 20000 logs/bot.log > logs/bot.log.tmp && mv logs/bot.log.tmp logs/bot.log
