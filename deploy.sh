#!/bin/bash
set -e

REPO_DIR="/opt/botrassilka"
VENV="$REPO_DIR/telegram-userbot-service/.venv/bin/pip"

echo "==> git pull"
cd "$REPO_DIR"
git pull origin main

echo "==> pip install -r requirements.txt"
"$VENV" install -r "$REPO_DIR/telegram-userbot-service/requirements.txt" --quiet

echo "==> pm2 restart"
pm2 restart portal userbot

echo "==> done"
pm2 list
