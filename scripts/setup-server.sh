#!/usr/bin/env bash
# Первичная настройка нового сервера.
# Устанавливает зависимости, клонирует репо, создаёт venv, собирает n8n-nodes.
# Запускать от root на чистом Ubuntu/Debian.
set -e

REPO_URL="git@github.com:sid00x23-ctrl/n8n-telegramautoaccs.git"
REPO_DIR="/home/n8n-telegramautoaccs"
N8N_VERSION="2.32.6"

echo "==> Устанавливаем системные пакеты"
apt-get update
apt-get install -y redis-server python3-venv python3-pip curl build-essential python3 sqlite3

echo "==> Включаем Redis"
systemctl enable redis-server
systemctl start redis-server

echo "==> Устанавливаем Node.js 22 (требуется для n8n 2.32+)"
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs
node --version
npm --version

echo "==> Устанавливаем PM2 глобально"
npm install -g pm2

echo "==> Устанавливаем n8n $N8N_VERSION"
# Используем /dev/shm как кэш npm чтобы не тратить дисковое место на временные файлы.
# n8n 2.32+ требует ~2.3GB в /usr/lib/node_modules/n8n — убедитесь что на диске есть место.
mount -o remount,size=4G /dev/shm 2>/dev/null || true
NODE_OPTIONS="--max-old-space-size=512" \
  npm install -g "n8n@${N8N_VERSION}" \
  --cache /dev/shm/npm-cache \
  --prefer-offline
rm -rf /dev/shm/npm-cache

echo "==> Клонируем репозиторий"
if [ ! -d "$REPO_DIR/.git" ]; then
  git clone "$REPO_URL" "$REPO_DIR"
else
  echo "  Репо уже существует, пропускаем clone"
fi

echo "==> Патчим n8n (enterprise-фичи + фикс /n8n/ роутинга)"
bash "$REPO_DIR/scripts/patch-n8n.sh"

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
echo "Следующий шаг: создать клиента через:"
echo "  bash $REPO_DIR/scripts/new-client.sh <client_id> <domain>"
