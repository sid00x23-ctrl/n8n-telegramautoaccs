const REPO = '/home/n8n-telegramautoaccs';
const CLIENT = `${REPO}/CLIENT_ID`;

module.exports = {
  apps: [
    {
      name: 'portal',
      script: `${REPO}/portal/.venv/bin/uvicorn`,
      args: 'main:app --host 0.0.0.0 --port 7000',
      cwd: `${REPO}/portal`,
      interpreter: 'none',
      env: {
        PORTAL_USERNAME: 'admin',
        PORTAL_PASSWORD: '',           // заполнить
        JWT_SECRET: '',                // заполнить: openssl rand -hex 32
        USERBOT_API_URL: 'http://127.0.0.1:8000',
        PM2_USERBOT_NAME: 'userbot',
        N8N_URL: 'https://DOMAIN/n8n/',
        N8N_INTERNAL_URL: 'http://127.0.0.1:5678',
        N8N_OWNER_EMAIL: '',           // заполнить
        N8N_OWNER_PASSWORD: '',        // заполнить
      },
    },
    {
      name: 'userbot',
      script: `${REPO}/telegram-userbot-service/main.py`,
      interpreter: `${REPO}/telegram-userbot-service/.venv/bin/python`,
      cwd: CLIENT,
      env: {
        HEADLESS: 'true',
        TELEGRAM_API_ID: '',           // заполнить
        TELEGRAM_API_HASH: '',         // заполнить
        N8N_WEBHOOK_URL: 'http://127.0.0.1:5678/webhook/telegram-incoming',
        N8N_DELIVERY_WEBHOOK_URL: 'http://127.0.0.1:5678/webhook/telegram-delivery',
      },
    },
    {
      name: 'n8n',
      script: 'n8n',
      interpreter: 'none',
      cwd: CLIENT,
      env: {
        N8N_USER_FOLDER: `${CLIENT}/n8n-data`,
        N8N_PORT: '5678',
        N8N_PROTOCOL: 'https',
        WEBHOOK_URL: 'https://DOMAIN/',
        N8N_EDITOR_BASE_URL: 'https://DOMAIN/n8n/',
        N8N_PATH: '/n8n/',
        N8N_DIAGNOSTICS_ENABLED: 'false',
        N8N_VERSION_NOTIFICATIONS_ENABLED: 'false',
        N8N_TRUST_PROXY: 'true',
        N8N_PROXY_HOPS: '1',
        EXECUTIONS_DATA_PRUNE: 'true',
        EXECUTIONS_DATA_MAX_AGE: '168',
        EXECUTIONS_DATA_MAX_COUNT: '100',
      },
    },
  ],
};
