#!/usr/bin/env bash
# Создать директорию нового клиента и запустить его через PM2.
# Использование: ./scripts/new-client.sh <client_id>
# Пример:        ./scripts/new-client.sh acme
set -e

CLIENT_ID="$1"

if [ -z "$CLIENT_ID" ]; then
  echo "Usage: $0 <client_id>"
  echo "Example: $0 acme"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLIENTS_DIR="/opt/clients"
CLIENT_DIR="$CLIENTS_DIR/$CLIENT_ID"

if [ -d "$CLIENT_DIR" ]; then
  echo "Error: $CLIENT_DIR already exists"
  exit 1
fi

echo "Creating client: $CLIENT_ID"
echo "  directory: $CLIENT_DIR"

# Создаём структуру каталогов
mkdir -p "$CLIENT_DIR/sessions"
mkdir -p "$CLIENT_DIR/n8n-data"

# Копируем ecosystem.config.js и подставляем CLIENT_ID
sed "s|/opt/clients/CLIENT_ID|$CLIENT_DIR|g" \
  "$REPO_DIR/client-template/ecosystem.config.js" \
  > "$CLIENT_DIR/ecosystem.config.js"

# Копируем шаблон .env
cp "$REPO_DIR/client-template/.env.example" "$CLIENT_DIR/.env"

# Копируем пример конфига аккаунтов
cp "$REPO_DIR/client-template/accounts_config.example.json" "$CLIENT_DIR/accounts_config.json"

echo ""
echo "Done! Next steps:"
echo "  1. Заполни $CLIENT_DIR/.env (TELEGRAM_API_ID, TELEGRAM_API_HASH)"
echo "  2. Заполни $CLIENT_DIR/ecosystem.config.js (пароли, JWT_SECRET, URLs)"
echo "  3. Заполни $CLIENT_DIR/accounts_config.json (аккаунты клиента)"
echo "  4. Скопируй session-файлы в $CLIENT_DIR/sessions/"
echo "  5. pm2 start $CLIENT_DIR/ecosystem.config.js"
echo "  6. pm2 save"
