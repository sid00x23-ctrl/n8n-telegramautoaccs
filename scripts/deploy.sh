#!/usr/bin/env bash
# Деплой на сервере: git pull + перезапуск сервисов.
# Запускать на сервере: bash /opt/botrassilka/scripts/deploy.sh
set -e

REPO_DIR="/opt/botrassilka"

echo "==> git pull"
cd "$REPO_DIR"
git pull

echo "==> Перезапускаем portal (с обновлением env)"
pm2 startOrRestart /opt/clients/client1/ecosystem.config.js --only portal

echo "==> Перезапускаем userbot (с обновлением env)"
pm2 startOrRestart /opt/clients/client1/ecosystem.config.js --only userbot

echo ""
echo "Готово. Статус:"
pm2 list --no-color
