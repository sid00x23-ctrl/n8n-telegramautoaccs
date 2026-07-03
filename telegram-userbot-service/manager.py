import asyncio
import json
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from telethon import TelegramClient, events
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PeerFloodError,
)
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import User, PeerUser, ReactionEmoji

from config import settings
from models import AccountConfig

logger = logging.getLogger(__name__)

CONFIGS_FILE    = Path("accounts_config.json")
SENT_CHATS_FILE = Path("sent_chats.json")


class AccountManager:
    def __init__(self):
        # account_id → TelegramClient
        self.clients: dict[str, TelegramClient] = {}
        # account_id → AccountConfig
        self.configs: dict[str, AccountConfig] = {}
        # account_id → True/False (кэш статуса авторизации)
        self.authorized: dict[str, bool] = {}
        # Временные данные для процесса авторизации
        # account_id → {"phone_code_hash": str, "phone": str}
        self._auth_state: dict[str, dict] = {}

        # account_id → set of chat_ids успешно отправленных сообщений
        self._sent_chats: dict[str, set[int]] = {}

        # Глобальная очередь приветствий (rate-limited отправки)
        self._greeting_queue: asyncio.Queue = asyncio.Queue()
        self._greeting_worker_task: Optional[asyncio.Task] = None
        self._last_greeting_time: float = 0.0
        self._greeting_interval: float = 180.0  # секунд между приветствиями

        self._load_configs()
        self._load_sent_chats()

    # ------------------------------------------------------------------ #
    #  Инициализация                                                       #
    # ------------------------------------------------------------------ #

    def _load_configs(self):
        if CONFIGS_FILE.exists():
            data = json.loads(CONFIGS_FILE.read_text())
            for item in data:
                cfg = AccountConfig(**item)
                self.configs[cfg.account_id] = cfg
            logger.info(f"Загружено {len(self.configs)} аккаунтов из конфига")

    def _save_configs(self):
        data = [cfg.model_dump(mode='json') for cfg in self.configs.values()]
        CONFIGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    def _load_sent_chats(self):
        if SENT_CHATS_FILE.exists():
            raw = json.loads(SENT_CHATS_FILE.read_text())
            self._sent_chats = {acc: set(ids) for acc, ids in raw.items()}
            logger.info(f"Загружено sent_chats для {len(self._sent_chats)} аккаунтов")

    def _save_sent_chats(self):
        raw = {acc: list(ids) for acc, ids in self._sent_chats.items()}
        SENT_CHATS_FILE.write_text(json.dumps(raw, ensure_ascii=False))

    # ------------------------------------------------------------------ #
    #  Запуск / остановка                                                  #
    # ------------------------------------------------------------------ #

    async def start_all(self):
        """Подключить все сохранённые аккаунты."""
        for account_id, cfg in self.configs.items():
            await self._connect_client(account_id, cfg.phone)
        self._greeting_worker_task = asyncio.create_task(self._greeting_worker())

    async def stop_all(self):
        if self._greeting_worker_task:
            self._greeting_worker_task.cancel()
        for account_id, client in list(self.clients.items()):
            try:
                await client.disconnect()
                logger.info(f"Аккаунт {account_id} отключён")
            except Exception as e:
                logger.error(f"Ошибка при отключении {account_id}: {e}")

    # ------------------------------------------------------------------ #
    #  Создание / подключение клиента                                      #
    # ------------------------------------------------------------------ #

    async def _connect_client(self, account_id: str, phone: str) -> TelegramClient:
        session_path = str(settings.SESSIONS_DIR / account_id)
        client = TelegramClient(session_path, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
        self.clients[account_id] = client

        try:
            await client.connect()
            is_auth = await client.is_user_authorized()
            self.authorized[account_id] = is_auth

            if is_auth:
                me: Optional[User] = await client.get_me()
                # Обновляем tg_id/имя/username в конфиге
                if account_id in self.configs:
                    self.configs[account_id].tg_id = me.id
                    self.configs[account_id].name = me.first_name
                    self.configs[account_id].username = me.username
                    self._save_configs()
                logger.info(f"Аккаунт {account_id} авторизован как {me.first_name} (@{me.username}) tg_id={me.id}")
                self._register_incoming_handler(client, account_id)
            else:
                logger.warning(f"Аккаунт {account_id} не авторизован")
        except Exception as e:
            logger.error(f"Не удалось подключить {account_id}: {e}")

        return client

    def _register_incoming_handler(self, client: TelegramClient, account_id: str):
        """Навешиваем обработчик входящих личных сообщений."""

        @client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
        async def on_message(event: events.NewMessage.Event):
            if not event.message.text:
                return  # игнорируем фото/стикеры/голосовые

            sender = await event.get_sender()
            payload = {
                "account_id": account_id,
                "chat_id": event.chat_id,
                "message_id": event.message.id,
                "from_user_id": sender.id if sender else None,
                "from_username": getattr(sender, "username", None),
                "from_name": getattr(sender, "first_name", ""),
                "message_text": event.message.text,
                "timestamp": event.message.date.isoformat(),
            }

            await self._forward_to_n8n(payload, account_id)

        logger.info(f"Обработчик входящих сообщений зарегистрирован для {account_id}")

    async def _send_delivery_callback(self, status: str, account_id: str, chat_id: int, error: str = None):
        """Сообщить n8n о результате отправки (sent / error)."""
        if not settings.N8N_DELIVERY_WEBHOOK_URL:
            return
        payload = {"status": status, "account_id": account_id, "chat_id": chat_id}
        if error:
            payload["error"] = error
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(settings.N8N_DELIVERY_WEBHOOK_URL, json=payload)
                logger.debug(f"[{account_id}] delivery callback: {status} chat_id={chat_id}")
        except Exception as e:
            logger.error(f"[{account_id}] Не удалось отправить delivery callback: {e}")

    async def _forward_to_n8n(self, payload: dict, account_id: str):
        try:
            async with httpx.AsyncClient(timeout=30) as http:
                resp = await http.post(settings.N8N_WEBHOOK_URL, json=payload)
                resp.raise_for_status()
                logger.debug(f"[{account_id}] Сообщение переслано в n8n: chat_id={payload['chat_id']}")
        except httpx.HTTPStatusError as e:
            logger.error(f"[{account_id}] n8n вернул ошибку {e.response.status_code}: {e.response.text}")
        except Exception as e:
            logger.error(f"[{account_id}] Не удалось переслать в n8n: {e}")

    # ------------------------------------------------------------------ #
    #  Авторизация нового аккаунта (двухшаговый flow)                     #
    # ------------------------------------------------------------------ #

    async def start_authorization(self, account_id: str, phone: str) -> dict:
        """Шаг 1: запросить SMS-код."""
        # Если аккаунт уже подключён но НЕ авторизован — старая сессия может
        # мешать: Telegram отправит код на «мёртвую» сессию из файла.
        # Поэтому отключаемся и удаляем файлы сессии перед новым запросом.
        if account_id in self.clients and not self.authorized.get(account_id):
            client = self.clients[account_id]
            try:
                await client.disconnect()
            except Exception:
                pass
            for suffix in [".session", ".session-journal"]:
                path = settings.SESSIONS_DIR / f"{account_id}{suffix}"
                if path.exists():
                    path.unlink()
                    logger.info(f"Удалён старый файл сессии: {path.name}")
            del self.clients[account_id]
            self.authorized.pop(account_id, None)

        if account_id not in self.clients:
            await self._connect_client(account_id, phone)

        client = self.clients[account_id]

        if not client.is_connected():
            await client.connect()

        try:
            result = await client.send_code_request(phone)
        except FloodWaitError as e:
            return {"status": "error", "message": f"Слишком много попыток. Подождите {e.seconds} сек."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        self._auth_state[account_id] = {
            "phone": phone,
            "phone_code_hash": result.phone_code_hash,
        }

        # Определяем куда пришёл код
        code_type_name = type(result.type).__name__
        CODE_TYPE_LABELS = {
            "SentCodeTypeApp": "в приложение Telegram",
            "SentCodeTypeSms": "по SMS",
            "SentCodeTypeCall": "звонком",
            "SentCodeTypeFlashCall": "flash-звонком",
            "SentCodeTypeMissedCall": "пропущенным звонком",
            "SentCodeTypeEmailCode": "на email",
            "SentCodeTypeSetUpEmailRequired": "требуется email",
        }
        code_via = CODE_TYPE_LABELS.get(code_type_name, code_type_name)

        logger.info(f"Код для {account_id} ({phone}) отправлен: {code_type_name}")

        # Сохраняем конфиг (phone может быть новым)
        if account_id not in self.configs:
            self.configs[account_id] = AccountConfig(account_id=account_id, phone=phone)
        self._save_configs()

        return {
            "status": "code_sent",
            "message": f"Код отправлен {code_via}",
            "code_via": code_via,
            "code_type": code_type_name,
        }

    async def resend_authorization_code(self, account_id: str) -> dict:
        """Повторная отправка кода другим методом (обычно SMS после App)."""
        if account_id not in self._auth_state:
            return {"status": "error", "message": "Нет активного запроса авторизации"}

        client = self.clients[account_id]
        state = self._auth_state[account_id]

        try:
            result = await client(ResendCodeRequest(
                phone_number=state["phone"],
                phone_code_hash=state["phone_code_hash"],
            ))
            # Обновляем phone_code_hash — он меняется при resend
            self._auth_state[account_id]["phone_code_hash"] = result.phone_code_hash

            code_type_name = type(result.type).__name__
            CODE_TYPE_LABELS = {
                "SentCodeTypeApp": "в приложение Telegram",
                "SentCodeTypeSms": "по SMS",
                "SentCodeTypeCall": "звонком",
                "SentCodeTypeFlashCall": "flash-звонком",
                "SentCodeTypeMissedCall": "пропущенным звонком",
            }
            code_via = CODE_TYPE_LABELS.get(code_type_name, code_type_name)
            logger.info(f"Код для {account_id} повторно отправлен: {code_type_name}")
            return {"status": "code_sent", "code_via": code_via, "code_type": code_type_name}
        except FloodWaitError as e:
            return {"status": "error", "message": f"Слишком много попыток. Подождите {e.seconds} сек."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def complete_authorization(
        self, account_id: str, code: str, password: Optional[str] = None
    ) -> dict:
        """Шаг 2: ввести код (и пароль 2FA если нужно)."""
        if account_id not in self._auth_state:
            return {"status": "error", "message": "Сначала вызови start_auth"}

        client = self.clients[account_id]
        state = self._auth_state[account_id]

        try:
            await client.sign_in(
                phone=state["phone"],
                code=code,
                phone_code_hash=state["phone_code_hash"],
            )
        except SessionPasswordNeededError:
            if not password:
                return {"status": "2fa_required", "message": "Аккаунт защищён 2FA. Передайте поле password."}
            try:
                await client.sign_in(password=password)
            except Exception as e:
                return {"status": "error", "message": f"Неверный пароль 2FA: {e}"}
        except PhoneCodeInvalidError:
            return {"status": "error", "message": "Неверный код. Попробуйте ещё раз."}
        except PhoneCodeExpiredError:
            return {"status": "error", "message": "Код истёк. Вызовите start_auth снова."}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        self.authorized[account_id] = True
        del self._auth_state[account_id]

        me: Optional[User] = await client.get_me()

        # Сохраняем tg_id/имя/username в конфиге
        cfg = self.configs[account_id]
        cfg.tg_id = me.id
        cfg.name = me.first_name
        cfg.username = me.username
        self._save_configs()

        self._register_incoming_handler(client, account_id)

        logger.info(f"Аккаунт {account_id} успешно авторизован как {me.first_name} tg_id={me.id}")
        return {
            "status": "authorized",
            "account_id": account_id,
            "tg_id": me.id,
            "name": me.first_name,
            "username": me.username,
            "phone": me.phone,
        }

    # ------------------------------------------------------------------ #
    #  QR-авторизация (альтернатива коду)                                  #
    # ------------------------------------------------------------------ #

    async def start_qr_authorization(self, account_id: str) -> dict:
        """Шаг 1: получить URL для QR-кода."""
        if account_id in self.clients and not self.authorized.get(account_id):
            client = self.clients[account_id]
            try:
                await client.disconnect()
            except Exception:
                pass
            for suffix in [".session", ".session-journal"]:
                path = settings.SESSIONS_DIR / f"{account_id}{suffix}"
                if path.exists():
                    path.unlink()
                    logger.info(f"Удалён старый файл сессии: {path.name}")
            del self.clients[account_id]
            self.authorized.pop(account_id, None)

        if account_id not in self.clients:
            session_path = str(settings.SESSIONS_DIR / account_id)
            client = TelegramClient(session_path, settings.TELEGRAM_API_ID, settings.TELEGRAM_API_HASH)
            self.clients[account_id] = client

        client = self.clients[account_id]
        if not client.is_connected():
            await client.connect()

        try:
            qr_login = await asyncio.wait_for(client.qr_login(), timeout=15)
        except asyncio.TimeoutError:
            return {"status": "error", "message": "Telegram не ответил за 15 сек — api_id заблокирован для всех методов авторизации"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        self._auth_state[account_id] = {"qr_login": qr_login}
        logger.info(f"QR-авторизация для {account_id}: URL получен")

        if account_id not in self.configs:
            self.configs[account_id] = AccountConfig(account_id=account_id, phone="")
            self._save_configs()

        return {"status": "qr_ready", "url": qr_login.url}

    async def refresh_qr_authorization(self, account_id: str) -> dict:
        """Обновить QR-код (если истёк)."""
        state = self._auth_state.get(account_id, {})
        qr_login = state.get("qr_login")
        if not qr_login:
            return {"status": "error", "message": "Нет активной QR-сессии"}
        try:
            await qr_login.recreate()
            logger.info(f"QR обновлён для {account_id}")
            return {"status": "qr_ready", "url": qr_login.url}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    async def wait_qr_authorization(self, account_id: str, password: Optional[str] = None) -> dict:
        """Ждать сканирования QR (30 сек). Повторяй до успеха или отмены."""
        state = self._auth_state.get(account_id, {})

        # 2FA уже была запрошена — просто вводим пароль
        if state.get("pending_2fa"):
            if not password:
                return {"status": "2fa_required", "message": "Нужен пароль 2FA"}
            try:
                await self.clients[account_id].sign_in(password=password)
            except Exception as e:
                return {"status": "error", "message": f"Неверный пароль 2FA: {e}"}
            return await self._finalize_qr_auth(account_id)

        qr_login = state.get("qr_login")
        if not qr_login:
            return {"status": "error", "message": "Нет активной QR-сессии"}

        try:
            await qr_login.wait(timeout=30)
        except asyncio.TimeoutError:
            return {"status": "timeout"}
        except SessionPasswordNeededError:
            if password:
                try:
                    await self.clients[account_id].sign_in(password=password)
                except Exception as e:
                    return {"status": "error", "message": f"Неверный пароль 2FA: {e}"}
                return await self._finalize_qr_auth(account_id)
            self._auth_state[account_id]["pending_2fa"] = True
            return {"status": "2fa_required", "message": "Аккаунт защищён 2FA"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

        return await self._finalize_qr_auth(account_id)

    async def _finalize_qr_auth(self, account_id: str) -> dict:
        """Финализация после успешного сканирования QR."""
        self.authorized[account_id] = True
        self._auth_state.pop(account_id, None)

        me: Optional[User] = await self.clients[account_id].get_me()
        cfg = self.configs.get(account_id)
        if cfg:
            cfg.tg_id = me.id
            cfg.name = me.first_name
            cfg.username = me.username
            cfg.phone = me.phone or cfg.phone
            self._save_configs()

        self._register_incoming_handler(self.clients[account_id], account_id)

        logger.info(f"QR-авторизация завершена: {account_id} = {me.first_name} tg_id={me.id}")
        return {
            "status": "authorized",
            "account_id": account_id,
            "tg_id": me.id,
            "name": me.first_name,
            "username": me.username,
            "phone": me.phone,
        }

    # ------------------------------------------------------------------ #
    #  Воркер очереди приветствий                                         #
    # ------------------------------------------------------------------ #

    async def _greeting_worker(self):
        """Фоновый воркер: отправляет приветствия по одному, раз в минуту."""
        logger.info("Greeting worker запущен")
        while True:
            account_id, chat_id, text, username = await self._greeting_queue.get()
            try:
                now = asyncio.get_event_loop().time()
                wait = self._greeting_interval - (now - self._last_greeting_time)
                if wait > 0:
                    logger.info(f"[greeting_worker] ждём {wait:.1f} сек перед отправкой {account_id} → {chat_id}")
                    await asyncio.sleep(wait)
                self._last_greeting_time = asyncio.get_event_loop().time()
                await self.send_message(account_id, chat_id, text, username, rate_limited=False)
                logger.info(f"[greeting_worker] приветствие отправлено: {account_id} → {chat_id}")
            except Exception as e:
                logger.error(f"[greeting_worker] ошибка отправки {account_id} → {chat_id}: {e}")
            finally:
                self._greeting_queue.task_done()

    # ------------------------------------------------------------------ #
    #  Отправка сообщения                                                  #
    # ------------------------------------------------------------------ #

    async def send_message(self, account_id: str, chat_id: int, text: str, username: Optional[str] = None, rate_limited: bool = False, instant: bool = False) -> dict:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден. Список: {list(self.clients.keys())}")

        client = self.clients[account_id]

        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")

        cfg = self.configs.get(account_id)
        if cfg and cfg.banned_until:
            remaining = cfg.banned_until - datetime.now(timezone.utc)
            if remaining.total_seconds() > 0:
                raise ValueError(
                    f"Аккаунт '{account_id}' ограничен спам-баном до {cfg.banned_until.isoformat()} "
                    f"(осталось {int(remaining.total_seconds() // 3600)}ч {int((remaining.total_seconds() % 3600) // 60)}мин)"
                )

        if rate_limited:
            await self._greeting_queue.put((account_id, chat_id, text, username))
            logger.info(f"[{account_id}] поставлено в очередь приветствий (в очереди: {self._greeting_queue.qsize()})")
            return {"status": "queued", "account_id": account_id, "chat_id": chat_id}

        # Заменяем длинные тире на короткие
        text = text.replace('—', '-').replace('–', '-')

        try:
            # Резолвим entity: если есть username — используем его (кешируется Telethon),
            # иначе пробуем по числовому chat_id
            entity = None
            clean_username = (username or "").strip().lstrip("@")
            if clean_username:
                try:
                    entity = await client.get_entity(clean_username)
                    logger.info(f"[{account_id}] Entity резолвнута по username @{clean_username} → id={entity.id}")
                except Exception as e:
                    logger.warning(f"[{account_id}] Не удалось резолвить @{clean_username}: {e}. Пробуем по chat_id.")

            if entity is None:
                entity = await client.get_entity(PeerUser(chat_id))

            if not instant:
                # 1. Отмечаем сообщение прочитанным
                try:
                    await client.send_read_acknowledge(entity)
                except Exception:
                    pass

                # 2. Пауза перед набором
                await asyncio.sleep(2)

                # 3. Typing 7–10 секунд
                typing_duration = random.uniform(7, 10)
                try:
                    async with client.action(entity, 'typing'):
                        await asyncio.sleep(typing_duration)
                except Exception:
                    pass

            # 4. Отправляем
            await client.send_message(entity, text)
            await self._send_delivery_callback("sent", account_id, chat_id)
            self._sent_chats.setdefault(account_id, set()).add(chat_id)
            self._save_sent_chats()
            return {"status": "sent", "account_id": account_id, "chat_id": chat_id}
        except FloodWaitError as e:
            await self._send_delivery_callback("error", account_id, chat_id, f"FloodWait: {e.seconds} сек")
            raise ValueError(f"FloodWait: подождите {e.seconds} секунд") from e
        except PeerFloodError as e:
            banned_until = datetime.now(timezone.utc) + timedelta(hours=24)
            cfg = self.configs.get(account_id)
            if cfg:
                cfg.banned_until = banned_until
                self._save_configs()
            logger.warning(f"[{account_id}] PeerFloodError — спам-бан. Аккаунт заблокирован до {banned_until.isoformat()}")
            await self._send_delivery_callback("error", account_id, chat_id, f"SpamBan: аккаунт ограничён на 24ч")
            raise ValueError(f"Аккаунт заблокирован спам-баном на 24 часа (до {banned_until.isoformat()})") from e
        except Exception as e:
            await self._send_delivery_callback("error", account_id, chat_id, str(e))
            raise ValueError(f"Ошибка отправки: {e}") from e

    # ------------------------------------------------------------------ #
    #  Выход из аккаунта                                                   #
    # ------------------------------------------------------------------ #

    async def logout(self, account_id: str):
        """Выйти из аккаунта и удалить сессию."""
        client = self.clients.get(account_id)
        if client:
            try:
                if await client.is_user_authorized():
                    await client.log_out()   # отзываем сессию на стороне Telegram
                else:
                    await client.disconnect()
            except Exception as e:
                logger.warning(f"Ошибка при выходе из {account_id}: {e}")
                try:
                    await client.disconnect()
                except Exception:
                    pass

        # Удаляем файлы сессии
        for suffix in [".session", ".session-journal"]:
            path = settings.SESSIONS_DIR / f"{account_id}{suffix}"
            if path.exists():
                path.unlink()

        # Чистим все словари
        self.clients.pop(account_id, None)
        self.configs.pop(account_id, None)
        self.authorized.pop(account_id, None)
        self._auth_state.pop(account_id, None)

        self._save_configs()
        logger.info(f"Аккаунт {account_id} удалён")

    async def logout_all(self):
        """Выйти из всех аккаунтов."""
        for account_id in list(self.clients.keys()):
            await self.logout(account_id)

    # ------------------------------------------------------------------ #
    #  Реакция на сообщение                                                #
    # ------------------------------------------------------------------ #

    async def react_to_message(self, account_id: str, chat_id: int, message_id: int, emoji: str = '❤', username: Optional[str] = None) -> dict:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")

        client = self.clients[account_id]
        try:
            entity = None
            clean_username = (username or "").strip().lstrip("@")
            if clean_username:
                try:
                    entity = await client.get_entity(clean_username)
                except Exception as e:
                    logger.warning(f"[{account_id}] react: не удалось резолвить @{clean_username}: {e}. Пробуем по chat_id.")
            if entity is None:
                entity = await client.get_entity(PeerUser(chat_id))
            await client.send_read_acknowledge(entity)
            await asyncio.sleep(2)
            await client(SendReactionRequest(
                peer=entity,
                msg_id=message_id,
                reaction=[ReactionEmoji(emoticon=emoji)],
            ))
            logger.info(f"[{account_id}] Реакция {emoji} на message_id={message_id} chat_id={chat_id}")
            return {"status": "reacted", "account_id": account_id, "chat_id": chat_id, "message_id": message_id}
        except Exception as e:
            raise ValueError(f"Ошибка реакции: {e}") from e

    # ------------------------------------------------------------------ #
    #  Удаление / редактирование сообщений                                 #
    # ------------------------------------------------------------------ #

    async def delete_message(self, account_id: str, chat_id: int, message_id: int) -> dict:
        """Удалить сообщение для всех (revoke=True)."""
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            entity = chat_id
        await client.delete_messages(entity, [message_id], revoke=True)
        logger.info(f"[{account_id}] Сообщение {message_id} удалено для всех в чате {chat_id}")
        return {"status": "deleted", "message_id": message_id}

    async def edit_message(self, account_id: str, chat_id: int, message_id: int, text: str) -> dict:
        """Редактировать отправленное сообщение."""
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            entity = chat_id
        await client.edit_message(entity, message_id, text)
        logger.info(f"[{account_id}] Сообщение {message_id} отредактировано в чате {chat_id}")
        return {"status": "edited", "message_id": message_id, "text": text}

    # ------------------------------------------------------------------ #
    #  Статус аккаунтов                                                    #
    # ------------------------------------------------------------------ #

    async def get_dialogs(self, account_id: str, limit: int = 30) -> list[dict]:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        dialogs = await client.get_dialogs(limit=limit)
        result = []
        for d in dialogs:
            entity = d.entity
            last_msg = d.message
            result.append({
                "id": d.id,
                "name": d.name or str(d.id),
                "username": getattr(entity, "username", None),
                "unread_count": d.unread_count,
                "last_message": {
                    "text": (last_msg.text or "")[:120],
                    "date": last_msg.date.isoformat(),
                    "out": last_msg.out,
                } if last_msg else None,
            })
        return result

    async def get_messages(self, account_id: str, chat_id: int, limit: int = 50, offset_id: int = 0) -> list[dict]:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            entity = chat_id
        kwargs: dict = {"limit": limit}
        if offset_id:
            kwargs["offset_id"] = offset_id
        msgs = await client.get_messages(entity, **kwargs)
        result = []
        for m in reversed(list(msgs)):
            result.append({
                "id": m.id,
                "text": m.text or "",
                "date": m.date.isoformat(),
                "out": m.out,
            })
        return result

    def get_status(self) -> list[dict]:
        result = []
        now = datetime.now(timezone.utc)
        for account_id, client in self.clients.items():
            cfg = self.configs.get(account_id)
            authorized = self.authorized.get(account_id, False)
            banned_until = cfg.banned_until if cfg else None
            is_banned = banned_until is not None and banned_until > now
            result.append({
                "account_id": account_id,
                "tg_id": cfg.tg_id if cfg else None,
                "phone": cfg.phone if cfg else "—",
                "name": cfg.name if cfg else "—",
                "username": cfg.username if cfg else None,
                "connected": client.is_connected(),
                "authorized": authorized,
                "available": authorized and not is_banned,
                "banned_until": banned_until.isoformat() if banned_until else None,
                "pending_auth": account_id in self._auth_state,
            })
        return result
