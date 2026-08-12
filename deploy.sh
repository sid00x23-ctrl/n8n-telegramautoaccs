#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/telegram-userbot-service/.venv"

echo "==> repo: $REPO_DIR"

echo "==> git pull"
cd "$REPO_DIR"
git pull origin main

echo "==> pip install"
"$VENV/bin/pip" install -r "$REPO_DIR/telegram-userbot-service/requirements.txt" --quiet

echo "==> pip install (portal)"
"$REPO_DIR/portal/.venv/bin/pip" install -r "$REPO_DIR/portal/requirements.txt" --quiet

echo "==> pm2 restart"
pm2 restart ecosystem.config.js --update-env

echo "==> done"
pm2 list
