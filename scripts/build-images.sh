#!/usr/bin/env bash
# Собрать Docker-образы из исходников репо.
# Запускать после git pull или изменений в коде.
set -e

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Building n8n image with custom nodes..."
docker build -t botrassilka-n8n "$REPO_DIR/n8n-nodes"

echo "==> Building userbot image..."
docker build -t botrassilka-userbot "$REPO_DIR/telegram-userbot-service"

echo ""
echo "Done. Images ready: botrassilka-n8n, botrassilka-userbot"
