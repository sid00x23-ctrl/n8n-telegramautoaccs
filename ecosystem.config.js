const REPO = '/home/n8n-telegramautoaccs';

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
        PORTAL_PASSWORD: 'Admin1234',
        JWT_SECRET: '889d067128b287b1f56f8916fade73fe62378cb59efd8cb8e5e7aa20ccf8e188',
        USERBOT_API_URL: 'http://127.0.0.1:8000',
        PM2_USERBOT_NAME: 'userbot',
        N8N_URL: 'https://anna12345ddd.jo3.org/n8n/',
        N8N_INTERNAL_URL: 'http://127.0.0.1:5678',
        N8N_OWNER_EMAIL: 'sid00x23@gmail.com',
        N8N_OWNER_PASSWORD: 'Admin1234',
      },
    },
    {
      name: 'userbot',
      script: `${REPO}/telegram-userbot-service/main.py`,
      interpreter: `${REPO}/telegram-userbot-service/.venv/bin/python`,
      cwd: `${REPO}/client1`,
      env: {
        HEADLESS: 'true',
        TELEGRAM_API_ID: '25567902',
        TELEGRAM_API_HASH: '0d1b129f7841ce77c2bfc726fcde54ff',
        N8N_WEBHOOK_URL: 'http://127.0.0.1:5678/webhook/telegram-incoming',
        N8N_DELIVERY_WEBHOOK_URL: 'http://127.0.0.1:5678/webhook/telegram-delivery',
      },
    },
    {
      name: 'n8n',
      script: 'n8n',
      interpreter: 'none',
      cwd: `${REPO}/client1`,
      env: {
        N8N_USER_FOLDER: `${REPO}/client1/n8n-data`,
        N8N_PORT: '5678',
        N8N_PROTOCOL: 'https',
        WEBHOOK_URL: 'https://anna12345ddd.jo3.org/',
        N8N_EDITOR_BASE_URL: 'https://anna12345ddd.jo3.org/n8n/',
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
