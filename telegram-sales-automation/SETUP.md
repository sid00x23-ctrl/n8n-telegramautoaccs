# Telegram Sales Automation — Инструкция по настройке

## Архитектура

```
Telethon-сервис (Python)          n8n
┌─────────────────────────┐      ┌──────────────────────────────────┐
│  Аккаунт 1 (session)    │      │  04 - Webhook → Ядро диалога     │
│  Аккаунт 2 (session)    │─────►│  03 - Ядро диалога (sub-wf)      │
│  Аккаунт N (session)    │◄─────│  02 - Планировщик рассылки       │
│                         │      │  01 - Назначение исполнителей    │
│  REST API :8000         │      └────────────┬─────────────────────┘
└─────────────────────────┘                   │
                                              ▼
                                       Google Sheets
```

**Поток данных:**
- Входящее сообщение: Telethon → POST /webhook/telegram-incoming → n8n workflow 04 → workflow 03
- Исходящее сообщение: n8n workflow 02/03 → POST /send → Telethon → клиент

---

## 1. Telethon-сервис

### Получить API ключи Telegram
1. Войти на https://my.telegram.org/apps
2. Создать приложение
3. Скопировать `api_id` и `api_hash`

Это **одни ключи на всё приложение** — не на каждый аккаунт.

### Запуск сервиса
```bash
cd telegram-userbot-service

# Скопировать и заполнить конфиг
cp .env.example .env
# Вставить TELEGRAM_API_ID, TELEGRAM_API_HASH, N8N_WEBHOOK_URL

# Установить зависимости
pip install -r requirements.txt

# Запустить
python main.py
```

### Авторизация каждого аккаунта (один раз)

```bash
# Шаг 1: запросить SMS-код
curl -X POST http://localhost:8000/accounts/account1/start_auth \
  -H "Content-Type: application/json" \
  -d '{"phone": "+79001234567"}'

# Шаг 2: ввести код из Telegram
curl -X POST http://localhost:8000/accounts/account1/complete_auth \
  -H "Content-Type: application/json" \
  -d '{"code": "12345"}'

# Если аккаунт с 2FA:
curl -X POST http://localhost:8000/accounts/account1/complete_auth \
  -H "Content-Type: application/json" \
  -d '{"code": "12345", "password": "мой_пароль"}'
```

После успешной авторизации сессия сохраняется в `sessions/account1.session` и автоматически подключается при перезапуске сервиса.

### Проверить статус аккаунтов
```bash
curl http://localhost:8000/accounts
```

---

## 2. Google Sheets

Создать таблицу с двумя листами.

**Лист "Клиенты"** (столбцы):
| telegram_id | name | phone | status | executor_bot_id | executor_name | assigned_at | booked_at | meeting_request | filter_tag |

**Лист "Исполнители"**:
| bot_id | name | active | hourly_limit |

Пример данных "Исполнители":
```
account1 | Анна | TRUE | 1
account2 | Иван | TRUE | 1
```

`bot_id` должен совпадать с `account_id` в Telethon-сервисе.

---

## 3. Импорт воркфлоу в n8n

**Порядок:**
1. `workflow_03_dialog_core.json` — импортировать первым, запомнить ID
2. `workflow_04_incoming_webhook.json` — открыть нод "Запускаем ядро диалога", вставить ID из п.1
3. `workflow_02_outreach_scheduler.json`
4. `workflow_01_assign_clients.json`
5. `workflow_05_followup.json`
6. `workflow_06_delivery_callback.json`

---

## 4. Variables в n8n

Перед настройкой credentials задать переменную адреса userbot-сервиса.

**Settings → Variables → Add Variable:**

| Name | Value |
|---|---|
| `USERBOT_URL` | `http://userbot:8000` |

> Значение `http://userbot:8000` — стандартное для Docker-деплоя. Docker-сеть автоматически резолвит имя сервиса `userbot` в нужный контейнер. Менять не нужно.
>
> В воркфлоу адрес используется как `{{ $vars.USERBOT_URL }}` — это позволяет деплоить одни и те же workflow-файлы на разных серверах без изменений.

---

## 5. Credentials в n8n

Создать в Settings → Credentials:

| Название | Тип |
|---|---|
| Google Sheets | Google Sheets OAuth2 API |
| DeepSeek | DeepSeek API |
| Redis | Redis |

**Telegram-credentials НЕ нужны** — весь Telegram работает через Telethon-сервис.

---

## 6. Что заменить в воркфлоу

| Плейсхолдер | Где | Что поставить |
|---|---|---|
| `ЗАМЕНИТЕ_НА_ID_ТАБЛИЦЫ` | workflow_01, 02, 03 | ID вашей Google Таблицы |
| `CREDENTIAL_ID` | все воркфлоу | ID credential из n8n |
| `[ТЕКСТ ПРИВЕТСТВИЯ]` | workflow_02 | реальный текст |
| `[ТЕКСТ ПОДРОБНОСТЕЙ]` | workflow_03 | описание продукта |
| `[ВЕЖЛИВОЕ ПРОЩАНИЕ]` | workflow_03 | прощальный текст |
| `[ССЫЛКА]` | workflow_03 | ссылка на закрытый чат |
| `[СИСТЕМНЫЙ ПРОМПТ]` | workflow_03, KB нод | описание продукта для AI |

---

## 7. Машина состояний диалога

```
assigned
    │ Планировщик (workflow_02) шлёт приветствие
    ▼
greeting_sent
    ├─► [interested]  → details_sent   (шлём подробности)
    ├─► [refuse]      → refused        (прощаемся, пишем в таблицу)
    └─► [question]    → KB → ответ → остаёмся в greeting_sent

details_sent
    ├─► [book_yes]    → ask_request    (пишем: записан + ссылка, СРАЗУ: вопрос про запрос)
    ├─► [refuse]      → refused        (прощаемся, пишем в таблицу)
    └─► [question]    → KB → ответ → остаёмся в details_sent

ask_request
    ├─► [has_request] → closed         (сохраняем запрос в таблицу)
    ├─► [no_request]  → closed
    └─► [question]    → KB → ответ → остаёмся в ask_request
```

---

## 8. Запуск в продакшне (systemd)

```ini
# /etc/systemd/system/tg-userbot.service
[Unit]
Description=Telegram Userbot Service
After=network.target

[Service]
WorkingDirectory=/path/to/telegram-userbot-service
ExecStart=/usr/bin/python3 main.py
EnvironmentFile=/path/to/telegram-userbot-service/.env
Restart=always
RestartSec=5
StandardInput=null

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable tg-userbot
systemctl start tg-userbot
```
