#!/bin/bash
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> repo: $REPO_DIR"

# --- venv: userbot ---
echo "==> creating venv for userbot"
python3 -m venv "$REPO_DIR/telegram-userbot-service/.venv"
"$REPO_DIR/telegram-userbot-service/.venv/bin/pip" install --upgrade pip --quiet
"$REPO_DIR/telegram-userbot-service/.venv/bin/pip" install -r "$REPO_DIR/telegram-userbot-service/requirements.txt" --quiet

# --- venv: portal ---
echo "==> creating venv for portal"
python3 -m venv "$REPO_DIR/portal/.venv"
"$REPO_DIR/portal/.venv/bin/pip" install --upgrade pip --quiet
"$REPO_DIR/portal/.venv/bin/pip" install -r "$REPO_DIR/portal/requirements.txt" --quiet

# --- директории ---
echo "==> creating directories"
mkdir -p "$REPO_DIR/client1/n8n-data"
mkdir -p "$REPO_DIR/telegram-userbot-service/sessions"

# --- pm2 ---
echo "==> installing pm2"
npm install -g pm2 --quiet

echo "==> starting services"
cd "$REPO_DIR"
pm2 start ecosystem.config.js

echo "==> saving pm2 + autostart"
pm2 save
pm2 startup | tail -1 | bash || echo "run 'pm2 startup' manually if needed"

echo "==> done"
pm2 list
