#!/usr/bin/env bash
# Первичная настройка нового сервера.
# Устанавливает зависимости, создаёт venv, собирает n8n-nodes.
# Запускать от root на чистом Ubuntu/Debian.
set -e

REPO_DIR="/opt/botrassilka"

echo "==> Устанавливаем системные пакеты"
apt-get update
apt-get install -y redis-server python3-venv python3-pip curl

echo "==> Включаем Redis"
systemctl enable redis-server
systemctl start redis-server

echo "==> Устанавливаем Node.js 20"
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs

echo "==> Устанавливаем n8n и PM2 глобально"
npm install -g n8n pm2

echo "==> Создаём venv для portal"
python3 -m venv "$REPO_DIR/portal/.venv"
"$REPO_DIR/portal/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/portal/.venv/bin/pip" install -r "$REPO_DIR/portal/requirements.txt"

echo "==> Создаём venv для telegram-userbot-service"
python3 -m venv "$REPO_DIR/telegram-userbot-service/.venv"
"$REPO_DIR/telegram-userbot-service/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/telegram-userbot-service/.venv/bin/pip" install -r "$REPO_DIR/telegram-userbot-service/requirements.txt"

echo "==> Собираем кастомные ноды n8n"
cd "$REPO_DIR/n8n-nodes"
npm install
npm run build

echo "==> Ограничиваем journald (макс 50M на диске)"
mkdir -p /etc/systemd/journald.conf.d
cp "$REPO_DIR/scripts/journald-limit.conf" /etc/systemd/journald.conf.d/limit.conf
systemctl restart systemd-journald
journalctl --vacuum-size=50M

echo "==> Настраиваем PM2 автозапуск при перезагрузке"
pm2 startup

echo ""
echo "Готово! Сервер настроен."
echo "Следующий шаг: создать клиента через scripts/new-client.sh"
