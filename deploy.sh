#!/bin/bash
set -e

REPO_DIR="/opt/botrassilka"
VENV="$REPO_DIR/telegram-userbot-service/.venv/bin/pip"

echo "==> git pull"
cd "$REPO_DIR"
git pull origin main

echo "==> pip install -r requirements.txt"
"$VENV" install -r "$REPO_DIR/telegram-userbot-service/requirements.txt" --quiet

echo "==> pm2-logrotate"
if ! pm2 list | grep -q pm2-logrotate; then
  pm2 install pm2-logrotate --silent
fi
pm2 set pm2-logrotate:max_size 20M
pm2 set pm2-logrotate:retain 3
pm2 set pm2-logrotate:compress true
pm2 set pm2-logrotate:dateFormat YYYY-MM-DD

echo "==> pm2 restart"
pm2 restart portal userbot

echo "==> done"
pm2 list
