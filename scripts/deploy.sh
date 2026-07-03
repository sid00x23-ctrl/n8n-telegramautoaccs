#!/usr/bin/env bash
# Деплой на сервере: git pull + перезапуск сервисов.
# Запускать на сервере: bash /opt/botrassilka/scripts/deploy.sh
set -e

REPO_DIR="/opt/botrassilka"

echo "==> git pull"
cd "$REPO_DIR"
git pull

echo "==> Перезапускаем portal"
pm2 restart portal

echo "==> Перезапускаем userbot"
pm2 restart userbot

echo ""
echo "Готово. Статус:"
pm2 list --no-color
