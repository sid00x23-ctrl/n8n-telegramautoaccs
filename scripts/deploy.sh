#!/usr/bin/env bash
# Деплой на сервере: git pull + перезапуск сервисов.
# Запускать на сервере: bash /home/n8n-telegramautoaccs/scripts/deploy.sh
set -e

REPO_DIR="/home/n8n-telegramautoaccs"

echo "==> git pull"
cd "$REPO_DIR"
git pull

echo "==> Перезапускаем сервисы"
pm2 startOrRestart "$REPO_DIR/ecosystem.config.js" --update-env

echo ""
echo "Готово. Статус:"
pm2 list --no-color
