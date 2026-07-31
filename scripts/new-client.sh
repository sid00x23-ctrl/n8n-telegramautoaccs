#!/usr/bin/env bash
# Создать директорию нового клиента и подготовить конфиги.
# Использование: ./scripts/new-client.sh <client_id> <domain>
# Пример:        ./scripts/new-client.sh client1 example.com
set -e

CLIENT_ID="$1"
DOMAIN="$2"

if [ -z "$CLIENT_ID" ] || [ -z "$DOMAIN" ]; then
  echo "Usage: $0 <client_id> <domain>"
  echo "Example: $0 client1 example.com"
  exit 1
fi

REPO_DIR="/home/n8n-telegramautoaccs"
CLIENT_DIR="$REPO_DIR/$CLIENT_ID"

if [ -d "$CLIENT_DIR" ]; then
  echo "Error: $CLIENT_DIR already exists"
  exit 1
fi

echo "Creating client: $CLIENT_ID (domain: $DOMAIN)"
echo "  directory: $CLIENT_DIR"

# Создаём структуру каталогов
mkdir -p "$CLIENT_DIR/sessions"
mkdir -p "$CLIENT_DIR/n8n-data"

# Копируем ecosystem.config.js и подставляем CLIENT_ID и DOMAIN
sed \
  -e "s|CLIENT_ID|$CLIENT_ID|g" \
  -e "s|DOMAIN|$DOMAIN|g" \
  "$REPO_DIR/client-template/ecosystem.config.js" \
  > "$CLIENT_DIR/ecosystem.config.js"

# Копируем пример конфига аккаунтов
cp "$REPO_DIR/client-template/accounts_config.example.json" "$CLIENT_DIR/accounts_config.json"

# Генерируем nginx конфиг из шаблона
NGINX_CONF="/etc/nginx/sites-enabled/$CLIENT_ID"
sed "s/DOMAIN/$DOMAIN/g" "$REPO_DIR/nginx/botrassilka.conf.template" > "$NGINX_CONF"
echo "  nginx config: $NGINX_CONF"

# Проверяем и перезагружаем nginx
nginx -t && systemctl reload nginx
echo "  nginx reloaded"

echo ""
echo "Done! Next steps:"
echo "  1. Получи SSL сертификат: certbot --nginx -d $DOMAIN"
echo "  2. Заполни $CLIENT_DIR/ecosystem.config.js (пароли, JWT_SECRET, URLs)"
echo "  3. Заполни $CLIENT_DIR/accounts_config.json (аккаунты клиента)"
echo "  4. Скопируй session-файлы в $CLIENT_DIR/sessions/"
echo "  5. pm2 start $CLIENT_DIR/ecosystem.config.js && pm2 save"
