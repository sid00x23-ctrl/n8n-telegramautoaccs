#!/usr/bin/env bash
# Создать директорию нового клиента на основе шаблона.
# Использование: ./scripts/new-client.sh <client_id> <n8n_port> <userbot_port>
# Пример:        ./scripts/new-client.sh acme 5679 8001
set -e

CLIENT_ID="$1"
N8N_PORT="${2}"
USERBOT_PORT="${3}"

if [ -z "$CLIENT_ID" ] || [ -z "$N8N_PORT" ] || [ -z "$USERBOT_PORT" ]; then
  echo "Usage: $0 <client_id> <n8n_port> <userbot_port>"
  echo "Example: $0 acme 5679 8001"
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
echo "  n8n port:     $N8N_PORT"
echo "  userbot port: $USERBOT_PORT"
echo "  directory:    $CLIENT_DIR"

mkdir -p "$CLIENT_DIR/sessions"

cp "$REPO_DIR/client-template/docker-compose.yml" "$CLIENT_DIR/"
cp "$REPO_DIR/client-template/.env.example"       "$CLIENT_DIR/.env"
cp "$REPO_DIR/client-template/accounts_config.example.json" "$CLIENT_DIR/accounts_config.json"

# Подставить значения в .env
sed -i "s/CLIENT_ID=client1/CLIENT_ID=$CLIENT_ID/"   "$CLIENT_DIR/.env"
sed -i "s/N8N_PORT=5678/N8N_PORT=$N8N_PORT/"          "$CLIENT_DIR/.env"
sed -i "s/USERBOT_PORT=8000/USERBOT_PORT=$USERBOT_PORT/" "$CLIENT_DIR/.env"

echo ""
echo "Done! Next steps:"
echo "  1. cd $CLIENT_DIR"
echo "  2. Заполни .env (TELEGRAM_API_ID, TELEGRAM_API_HASH)"
echo "  3. Заполни accounts_config.json (аккаунты клиента)"
echo "  4. Скопируй session-файлы в sessions/"
echo "  5. docker compose up -d"
