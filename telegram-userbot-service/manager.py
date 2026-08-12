import asyncio
import json
import logging
import random
import socks
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlparse, parse_qs

import httpx
from telethon import TelegramClient, events
from telethon.network.connection import (
    ConnectionTcpMTProxyRandomizedIntermediate,
    ConnectionTcpAbridged,
)
from fake_tls_mtproxy import ConnectionTcpMTProxyFakeTLS
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    FloodWaitError,
    PeerFloodError,
)
from telethon.tl.functions.auth import ResendCodeRequest
from telethon.tl.functions.messages import SendReactionRequest
from telethon.tl.types import (
    User, PeerUser, ReactionEmoji,
    UserStatusOnline, UserStatusOffline,
    UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth, UserStatusEmpty,
)

from config import settings
from models import AccountConfig
from proxy_manager import ProxyPool

logger = logging.getLogger(__name__)

# Список реальных Android-устройств для имитации официального клиента.
# Каждый аккаунт получает одно устройство детерминированно по account_id.
_ANDROID_DEVICES = [
    ("Samsung SM-S918B", "Android 14"),
    ("Samsung SM-G991B", "Android 13"),
    ("Google Pixel 8 Pro", "Android 14"),
    ("Google Pixel 7a", "Android 13"),
    ("Xiaomi 2210132G", "Android 13"),
    ("Xiaomi 22071212AG", "Android 13"),
    ("OnePlus CPH2447", "Android 14"),
    ("Samsung SM-A546E", "Android 13"),
    ("Samsung SM-F946B", "Android 14"),
    ("POCO X6 Pro", "Android 13"),
]
_TG_APP_VERSION = "10.14.5"


def _get_device_params(account_id: str) -> dict:
    """Возвращает детерминированные параметры устройства для аккаунта.

    Один и тот же account_id всегда даёт одинаковый fingerprint,
    но разные аккаунты получают разные устройства.
    """
    rng = random.Random(account_id)
    device_model, system_version = rng.choice(_ANDROID_DEVICES)
    return {
        "device_model": device_model,
        "system_version": system_version,
        "app_version": _TG_APP_VERSION,
        "lang_code": "ru",
        "system_lang_code": "ru-RU",
    }


CONFIGS_FILE    = Path("accounts_config.json")
SENT_CHATS_FILE = Path("sent_chats.json")


def _parse_proxy(proxy_url: Optional[str]) -> Tuple[Optional[tuple], Optional[type]]:
    """Парсит прокси URL. Возвращает (proxy_tuple, connection_class).

    Форматы:
      https://t.me/proxy?server=H&port=P&secret=S  → MTProto
      tg://proxy?server=H&port=P&secret=S           → MTProto
      ip:port                                        → HTTP без авторизации
      user:pass@ip:port                              → HTTP с авторизацией
      socks5://user:pass@ip:port                     → SOCKS5
      http://ip:port                                 → HTTP
    """
    if not proxy_url:
        return None, None

    # MTProto: t.me/proxy или tg://proxy
    proxy_url_stripped = proxy_url.strip()
    if "t.me/proxy" in proxy_url_stripped or proxy_url_stripped.startswith("tg://proxy"):
        if "t.me/proxy" in proxy_url_stripped and "://" not in proxy_url_stripped:
            proxy_url_stripped = "https://" + proxy_url_stripped
        p = urlparse(proxy_url_stripped)
        qs = parse_qs(p.query)
        server = str((qs.get("server") or qs.get("host") or [""])[0]).strip()
        port_str = str((qs.get("port") or [""])[0]).strip()
        secret_hex = str((qs.get("secret") or [""])[0]).strip()
        if not server or not port_str or not secret_hex:
            raise ValueError("MTProto прокси: нужны параметры server, port, secret")
        port = int(port_str)
        # Телеграм принимает секрет в нескольких форматах:
        # 1. Hex без префикса: <32 hex>           → Randomized Intermediate
        # 2. Hex: dd<32 hex>                       → Randomized Intermediate
        # 3. Hex: ee<32 hex>[<domain hex>]         → Fake-TLS
        # 4. Base64url целой строки (toproxylab):
        #    - starts with "ee" → Fake-TLS; starts with "dd" → Randomized
        #    - decoded first byte 0xEE → Fake-TLS; 0xDD → Randomized
        import base64 as _b64

        is_fake_tls = False

        if not all(c in "0123456789abcdefABCDEF" for c in secret_hex):
            decoded_bytes = None
            try:
                padding = (4 - len(secret_hex) % 4) % 4
                decoded_bytes = _b64.urlsafe_b64decode(secret_hex + "=" * padding)
            except Exception:
                pass
            if decoded_bytes is None:
                raise ValueError(f"Неверный secret в MTProto прокси: {secret_hex}")
            if len(decoded_bytes) >= 17 and decoded_bytes[0] in (0xEE, 0xDD):
                # Тип определяем по декодированному первому байту (не по строке)
                if decoded_bytes[0] == 0xEE:
                    is_fake_tls = True
                prefix = "ee" if decoded_bytes[0] == 0xEE else "dd"
                secret_hex = prefix + decoded_bytes[1:17].hex()
            elif len(decoded_bytes) >= 16:
                secret_hex = decoded_bytes[:16].hex()
            else:
                raise ValueError(
                    f"MTProto secret слишком короткий после base64url: {len(decoded_bytes)} байт"
                )
        else:
            # Чистый hex — тип определяется по text-префиксу
            if secret_hex[:2].lower() == "ee":
                is_fake_tls = True

        # Для fake-TLS прокси сохраняем полный секрет (вместе с доменом),
        # чтобы ConnectionTcpMTProxyFakeTLS мог извлечь SNI из него.
        # Для остальных типов обрезаем до минимальной длины.
        has_prefix = secret_hex[:2].lower() in ("dd", "ee")
        min_expected = 34 if has_prefix else 32
        if not is_fake_tls and len(secret_hex) > min_expected:
            secret_hex = secret_hex[:min_expected]
        if len(secret_hex) < min_expected:
            raise ValueError(
                f"MTProto secret слишком короткий: {len(secret_hex) // 2} байт "
                f"(нужно минимум {'17' if has_prefix else '16'})"
            )

        conn_class = ConnectionTcpMTProxyFakeTLS if is_fake_tls else ConnectionTcpMTProxyRandomizedIntermediate
        return (server, port, secret_hex), conn_class

    # Голый ip:port или user:pass@ip:port — добавляем схему http://
    if "://" not in proxy_url_stripped:
        proxy_url_stripped = "http://" + proxy_url_stripped
    p = urlparse(proxy_url_stripped)
    scheme = p.scheme.lower()
    if scheme == "socks5":
        proxy_type = socks.SOCKS5
    elif scheme == "socks4":
        proxy_type = socks.SOCKS4
    elif scheme in ("http", "https"):
        proxy_type = socks.HTTP
    else:
        raise ValueError(f"Неизвестный тип прокси: {scheme}. Используйте socks5://, socks4:// или http://")
    host = p.hostname
    port = p.port
    username = p.username or None
    password = p.password or None
    if not host or not port:
        raise ValueError(f"Неверный формат прокси URL: {proxy_url}")
    return (proxy_type, host, port, True, username, password), None


class AccountManager:
    def __init__(
        self,
        proxy_pool: Optional[ProxyPool] = None,
        configs_file: Optional[Path] = None,
        sessions_dir: Optional[Path] = None,
        sent_chats_file: Optional[Path] = None,
    ):
        self._configs_file = configs_file or CONFIGS_FILE
        self._sent_chats_file = sent_chats_file or SENT_CHATS_FILE
        self._sessions_dir = sessions_dir or settings.SESSIONS_DIR
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

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

        # account_id → сообщение об ошибке прокси (None = OK)
        self._proxy_errors: dict[str, str] = {}

        self.proxy_pool: Optional[ProxyPool] = proxy_pool
        self._load_configs()
        self._load_sent_chats()

    # ------------------------------------------------------------------ #
    #  Инициализация                                                       #
    # ------------------------------------------------------------------ #

    def _load_configs(self):
        if self._configs_file.exists():
            data = json.loads(self._configs_file.read_text())
            for item in data:
                cfg = AccountConfig(**item)
                self.configs[cfg.account_id] = cfg
            logger.info(f"Загружено {len(self.configs)} аккаунтов из {self._configs_file.name}")

    def _save_configs(self):
        data = [cfg.model_dump(mode='json') for cfg in self.configs.values()]
        self._configs_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _parse_status(status) -> dict:
        """Конвертирует Telethon UserStatus в простой dict."""
        if isinstance(status, UserStatusOnline):
            return {"type": "online"}
        if isinstance(status, UserStatusOffline):
            return {"type": "offline", "was_online": status.was_online.isoformat()}
        if isinstance(status, UserStatusRecently):
            return {"type": "recently"}
        if isinstance(status, UserStatusLastWeek):
            return {"type": "last_week"}
        if isinstance(status, UserStatusLastMonth):
            return {"type": "last_month"}
        if isinstance(status, UserStatusEmpty):
            return {"type": "long_ago"}
        return {"type": "unknown"}

    def _load_sent_chats(self):
        if self._sent_chats_file.exists():
            raw = json.loads(self._sent_chats_file.read_text())
            self._sent_chats = {acc: set(ids) for acc, ids in raw.items()}
            logger.info(f"Загружено sent_chats для {len(self._sent_chats)} аккаунтов")

    def _save_sent_chats(self):
        raw = {acc: list(ids) for acc, ids in self._sent_chats.items()}
        self._sent_chats_file.write_text(json.dumps(raw, ensure_ascii=False))

    # ------------------------------------------------------------------ #
    #  Запуск / остановка                                                  #
    # ------------------------------------------------------------------ #

    async def start_all(self):
        """Подключить все сохранённые аккаунты."""
        # Синхронизируем прокси из пула перед подключением клиентов
        if self.proxy_pool:
            for account_id, cfg in self.configs.items():
                proxy = self.proxy_pool.get_account_proxy(account_id)
                if not proxy:
                    proxy = self.proxy_pool.assign_proxy_to_account(account_id)
                    if proxy:
                        logger.info(f"[{account_id}] Автоназначен прокси {proxy['id'][:8]}...")
                if proxy:
                    # Проверяем совместимость с Telethon перед назначением
                    try:
                        _parse_proxy(proxy["url"])
                        cfg.proxy = proxy["url"]
                    except ValueError as e:
                        logger.warning(f"[{account_id}] Прокси {proxy['id'][:8]}... несовместим: {e} — оставляем без изменений")
            self._save_configs()
        sem = asyncio.Semaphore(10)

        async def _connect_one(account_id: str, cfg) -> None:
            async with sem:
                try:
                    await self._connect_client(account_id, cfg.phone)
                except Exception as e:
                    logger.error(f"[{account_id}] Ошибка подключения: {e}")

        await asyncio.gather(*[_connect_one(aid, cfg) for aid, cfg in self.configs.items()])
        if self.proxy_pool:
            self.proxy_pool.set_reconnect_callback(self._reconnect_to_proxy)
            await self.proxy_pool.start_monitor()

    async def stop_all(self):
        if self.proxy_pool:
            await self.proxy_pool.stop_monitor()
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
        session_path = str(self._sessions_dir / account_id)
        cfg = self.configs.get(account_id)
        try:
            proxy, connection_class = _parse_proxy(cfg.proxy if cfg else None)
        except ValueError as e:
            logger.error(f"[{account_id}] Неверный формат прокси: {e} — подключаемся без прокси")
            self._proxy_errors[account_id] = str(e)
            proxy, connection_class = None, None
        kwargs = {}
        if proxy:
            kwargs["proxy"] = proxy
        if connection_class:
            kwargs["connection"] = connection_class
        else:
            kwargs["connection"] = ConnectionTcpAbridged
        client = TelegramClient(
            session_path,
            settings.TELEGRAM_API_ID,
            settings.TELEGRAM_API_HASH,
            **_get_device_params(account_id),
            **kwargs,
        )
        self.clients[account_id] = client

        try:
            await asyncio.wait_for(client.connect(), timeout=30)
            is_auth = await asyncio.wait_for(client.is_user_authorized(), timeout=15)
            self.authorized[account_id] = is_auth

            if is_auth:
                me: Optional[User] = await asyncio.wait_for(client.get_me(), timeout=15)
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
        except asyncio.TimeoutError as e:
            logger.error(f"Не удалось подключить {account_id}: таймаут подключения (прокси завис?)")
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
                "user_id": event.chat_id,
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
                logger.debug(f"[{account_id}] Сообщение переслано в n8n: user_id={payload['user_id']}")
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
                path = self._sessions_dir / f"{account_id}{suffix}"
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

        # Назначаем прокси если ещё нет
        if self.proxy_pool and not cfg.proxy:
            proxy = self.proxy_pool.get_account_proxy(account_id) or self.proxy_pool.get_best_proxy()
            if proxy:
                try:
                    cfg.proxy = proxy["url"]
                    self._save_configs()
                    self.proxy_pool.assign_proxy_to_account(account_id, proxy_id=proxy["id"])
                    asyncio.create_task(self._reconnect_to_proxy(account_id, proxy["url"]))
                    logger.info(f"[{account_id}] Автоназначен прокси после авторизации: {proxy['id'][:8]}...")
                except Exception as e:
                    logger.warning(f"[{account_id}] Не удалось назначить прокси после авторизации: {e}")

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
                path = self._sessions_dir / f"{account_id}{suffix}"
                if path.exists():
                    path.unlink()
                    logger.info(f"Удалён старый файл сессии: {path.name}")
            del self.clients[account_id]
            self.authorized.pop(account_id, None)

        if account_id not in self.clients:
            session_path = str(self._sessions_dir / account_id)
            client = TelegramClient(
                session_path,
                settings.TELEGRAM_API_ID,
                settings.TELEGRAM_API_HASH,
                connection=ConnectionTcpAbridged,
                **_get_device_params(account_id),
            )
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

            # Назначаем прокси если ещё нет
            if self.proxy_pool and not cfg.proxy:
                proxy = self.proxy_pool.get_account_proxy(account_id) or self.proxy_pool.get_best_proxy()
                if proxy:
                    try:
                        cfg.proxy = proxy["url"]
                        self._save_configs()
                        self.proxy_pool.assign_proxy_to_account(account_id, proxy_id=proxy["id"])
                        asyncio.create_task(self._reconnect_to_proxy(account_id, proxy["url"]))
                        logger.info(f"[{account_id}] Автоназначен прокси после QR-авторизации: {proxy['id'][:8]}...")
                    except Exception as e:
                        logger.warning(f"[{account_id}] Не удалось назначить прокси после QR-авторизации: {e}")

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
    #  Отправка сообщения                                                  #
    # ------------------------------------------------------------------ #

    async def send_message(self, account_id: str, chat_id: Optional[int], text: str, username: Optional[str] = None, rate_limited: bool = False, instant: bool = False) -> dict:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден. Список: {list(self.clients.keys())}")

        client = self.clients[account_id]
        await self._ensure_proxy(account_id)
        client = self.clients[account_id]  # обновляем после возможного переподключения

        if not self.authorized.get(account_id):
            # Пробуем failover на следующий прокси и переподключаемся
            cfg = self.configs.get(account_id)
            if self.proxy_pool and cfg:
                cur_proxy = self.proxy_pool.get_account_proxy(account_id)
                proxy_id_to_exclude = cur_proxy["id"] if cur_proxy else None
                new_proxy = await self.proxy_pool.failover(account_id, proxy_id_to_exclude) if proxy_id_to_exclude else self.proxy_pool.get_best_proxy()
                if new_proxy:
                    logger.warning(f"[{account_id}] Не авторизован, пробуем следующий прокси {new_proxy['id'][:8]}...")
                    cfg.proxy = new_proxy["url"]
                    self._save_configs()
                    existing = self.clients.get(account_id)
                    if existing:
                        try:
                            await existing.disconnect()
                        except Exception:
                            pass
                    await self._connect_client(account_id, cfg.phone)
                else:
                    logger.warning(f"[{account_id}] Не авторизован, нет доступных прокси для failover")
            elif cfg:
                logger.warning(f"[{account_id}] Не авторизован, пробуем переподключиться...")
                existing = self.clients.get(account_id)
                if existing:
                    try:
                        await existing.disconnect()
                    except Exception:
                        pass
                await self._connect_client(account_id, cfg.phone)
            if not self.authorized.get(account_id):
                raise ValueError(f"Аккаунт '{account_id}' не авторизован")

        cfg = self.configs.get(account_id)
        # Для rate_limited (рассылка новым) — блокируем при бане.
        # Для прямых отправок в существующие диалоги — разрешаем, Telegram сам решит.
        if rate_limited and cfg and cfg.banned_until:
            remaining = cfg.banned_until - datetime.now(timezone.utc)
            if remaining.total_seconds() > 0:
                raise ValueError(
                    f"Аккаунт '{account_id}' ограничен спам-баном до {cfg.banned_until.isoformat()} "
                    f"(осталось {int(remaining.total_seconds() // 3600)}ч {int((remaining.total_seconds() % 3600) // 60)}мин)"
                )

        # Заменяем длинные тире на короткие
        text = text.replace('—', '-').replace('–', '-')

        try:
            # Резолвим entity: если есть username — используем его (кешируется Telethon),
            # иначе пробуем по числовому chat_id
            entity = None
            clean_username = (username or "").strip().lstrip("@")
            if clean_username:
                try:
                    entity = await asyncio.wait_for(client.get_entity(clean_username), timeout=30)
                    logger.info(f"[{account_id}] Entity резолвнута по username @{clean_username} → id={entity.id}")
                except (asyncio.TimeoutError, ConnectionError) as e:
                    logger.warning(f"[{account_id}] Ошибка соединения при резолве @{clean_username}: {e} — перебираем рабочие прокси...")
                    cfg_fo = self.configs.get(account_id)
                    if self.proxy_pool and cfg_fo:
                        tried_proxy_ids: set = set()
                        cur_proxy = self.proxy_pool.get_account_proxy(account_id)
                        if cur_proxy:
                            tried_proxy_ids.add(cur_proxy["id"])
                        while entity is None:
                            next_proxy = self.proxy_pool.get_best_proxy(exclude_ids=tried_proxy_ids)
                            if not next_proxy:
                                logger.warning(f"[{account_id}] Все рабочие прокси перебраны, entity не резолвилась. Пробуем по chat_id.")
                                break
                            logger.info(f"[{account_id}] Переключаемся на прокси {next_proxy['id'][:8]}...")
                            cfg_fo.proxy = next_proxy["url"]
                            self._save_configs()
                            existing = self.clients.get(account_id)
                            if existing:
                                try:
                                    await existing.disconnect()
                                except Exception:
                                    pass
                            await self._connect_client(account_id, cfg_fo.phone)
                            client = self.clients[account_id]
                            try:
                                entity = await asyncio.wait_for(client.get_entity(clean_username), timeout=30)
                                logger.info(f"[{account_id}] Entity резолвнута через прокси {next_proxy['id'][:8]} @{clean_username} → id={entity.id}")
                            except (asyncio.TimeoutError, ConnectionError) as retry_e:
                                logger.warning(f"[{account_id}] Прокси {next_proxy['id'][:8]} ошибка соединения: {retry_e}, пробуем следующий...")
                                tried_proxy_ids.add(next_proxy["id"])
                            except Exception as retry_e:
                                logger.warning(f"[{account_id}] Прокси {next_proxy['id'][:8]} ошибка резолва: {retry_e}, пробуем следующий...")
                                tried_proxy_ids.add(next_proxy["id"])
                    else:
                        logger.warning(f"[{account_id}] Proxy pool недоступен. Пробуем по chat_id.")
                except Exception as e:
                    logger.warning(f"[{account_id}] Не удалось резолвить @{clean_username}: {e}. Пробуем по chat_id.")

            if entity is None:
                if chat_id is None:
                    raise ValueError("Необходимо указать chat_id или username/lastsender_id для определения получателя")
                entity = await asyncio.wait_for(client.get_entity(PeerUser(chat_id)), timeout=30)

            if not instant:
                # 1. Отмечаем сообщение прочитанным
                try:
                    await client.send_read_acknowledge(entity)
                except Exception:
                    pass

                cfg = self.configs.get(account_id)
                if cfg and cfg.typing_enabled:
                    # 2. Пауза перед набором
                    await asyncio.sleep(2)

                    # 3. Typing (длительность из настроек аккаунта)
                    t_min = cfg.typing_min_seconds
                    t_max = max(cfg.typing_max_seconds, t_min)
                    typing_duration = random.uniform(t_min, t_max)
                    try:
                        async with client.action(entity, 'typing'):
                            await asyncio.sleep(typing_duration)
                    except Exception:
                        pass

            # 4. Отправляем
            cfg = self.configs.get(account_id)
            link_preview = not (cfg.link_preview_disabled if cfg else False)
            await asyncio.wait_for(client.send_message(entity, text, link_preview=link_preview), timeout=60)
            await self._send_delivery_callback("sent", account_id, chat_id)
            self._sent_chats.setdefault(account_id, set()).add(chat_id)
            self._save_sent_chats()
            return {"status": "sent", "account_id": account_id, "chat_id": chat_id}
        except FloodWaitError as e:
            await self._send_delivery_callback("error", account_id, chat_id, f"FloodWait: {e.seconds} сек")
            raise ValueError(f"FloodWait: подождите {e.seconds} секунд") from e
        except PeerFloodError as e:
            cfg = self.configs.get(account_id)
            if cfg and cfg.spam_ban_auto:
                banned_until = datetime.now(timezone.utc) + timedelta(hours=24)
                cfg.banned_until = banned_until
                self._save_configs()
                logger.warning(f"[{account_id}] PeerFloodError — спам-бан выставлен до {banned_until.isoformat()}")
                await self._send_delivery_callback("error", account_id, chat_id, f"SpamBan: аккаунт ограничён на 24ч")
                raise ValueError(f"Аккаунт заблокирован спам-баном на 24 часа (до {banned_until.isoformat()})") from e
            else:
                logger.warning(f"[{account_id}] PeerFloodError — авто-бан отключён, бан не выставлен")
                await self._send_delivery_callback("error", account_id, chat_id, "SpamBan: PeerFloodError (авто-бан отключён)")
                raise ValueError("PeerFloodError: Telegram ограничил отправку (авто-бан отключён)") from e
        except Exception as e:
            # Если соединение потеряно — failover прокси и повтор
            _client_now = self.clients.get(account_id)
            _conn_lost = (
                not _client_now or not _client_now.is_connected() or
                isinstance(e, (ConnectionError, OSError, asyncio.TimeoutError))
            )
            if _conn_lost and self.proxy_pool:
                _cur_proxy = self.proxy_pool.get_account_proxy(account_id)
                if _cur_proxy:
                    logger.warning(f"[{account_id}] Потеря соединения при отправке, failover...")
                    _cfg_r = self.configs.get(account_id)
                    _new_prx = await self.proxy_pool.failover(account_id, _cur_proxy["id"])
                    if _new_prx and _cfg_r:
                        _cfg_r.proxy = _new_prx["url"]
                        self._save_configs()
                        await self._connect_client(account_id, _cfg_r.phone)
                        _rc = self.clients.get(account_id)
                        if _rc and _rc.is_connected():
                            try:
                                _re = entity if entity is not None else PeerUser(chat_id)
                                _lp = not (_cfg_r.link_preview_disabled if _cfg_r else False)
                                await _rc.send_message(_re, text, link_preview=_lp)
                                await self._send_delivery_callback("sent", account_id, chat_id)
                                self._sent_chats.setdefault(account_id, set()).add(chat_id)
                                self._save_sent_chats()
                                logger.info(f"[{account_id}] Retry после failover успешен")
                                return {"status": "sent", "account_id": account_id, "chat_id": chat_id}
                            except Exception as _re_e:
                                logger.error(f"[{account_id}] Retry не удался: {_re_e}")
            await self._send_delivery_callback("error", account_id, chat_id, str(e))
            raise ValueError(f"Ошибка отправки: {e}") from e

    # ------------------------------------------------------------------ #
    #  Настройки typing                                                    #
    # ------------------------------------------------------------------ #

    def update_typing_settings(
        self,
        account_id: str,
        typing_enabled: Optional[bool] = None,
        typing_min_seconds: Optional[float] = None,
        typing_max_seconds: Optional[float] = None,
    ) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if typing_enabled is not None:
            cfg.typing_enabled = typing_enabled
        if typing_min_seconds is not None:
            cfg.typing_min_seconds = typing_min_seconds
        if typing_max_seconds is not None:
            cfg.typing_max_seconds = typing_max_seconds
        self._save_configs()
        return {
            "account_id": account_id,
            "typing_enabled": cfg.typing_enabled,
            "typing_min_seconds": cfg.typing_min_seconds,
            "typing_max_seconds": cfg.typing_max_seconds,
        }

    # ------------------------------------------------------------------ #
    #  Настройки превью ссылок                                             #
    # ------------------------------------------------------------------ #

    async def _reconnect_to_proxy(self, account_id: str, new_proxy_url: str):
        """
        Callback для proxy monitor: переподключает аккаунт через новый прокси.
        Вызывается при падении прокси и при балансировке нагрузки.
        """
        cfg = self.configs.get(account_id)
        if not cfg:
            logger.warning(f"[{account_id}] _reconnect_to_proxy: аккаунт не найден в конфигах")
            return
        cfg.proxy = new_proxy_url
        self._save_configs()
        existing = self.clients.get(account_id)
        if existing:
            try:
                await existing.disconnect()
            except Exception:
                pass
        await self._connect_client(account_id, cfg.phone)
        logger.info(f"[{account_id}] Переподключён через новый прокси")

    async def _ensure_proxy(self, account_id: str):
        """
        Проверяет прокси аккаунта перед операцией.
        Если прокси помечен как error — делает failover и переподключает клиент.
        """
        if not self.proxy_pool:
            return
        proxy = self.proxy_pool.get_account_proxy(account_id)
        if not proxy or proxy["status"] != "error":
            return
        logger.warning(f"[{account_id}] Прокси {proxy['id'][:8]}... error — выполняем failover...")
        cfg = self.configs.get(account_id)
        new_proxy = await self.proxy_pool.failover(account_id, proxy["id"])
        if new_proxy and cfg:
            cfg.proxy = new_proxy["url"]
            self._save_configs()
            client = self.clients.get(account_id)
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await self._connect_client(account_id, cfg.phone)
            self._proxy_errors.pop(account_id, None)
            logger.info(f"[{account_id}] Переключён на прокси {new_proxy['id'][:8]}...")
        else:
            logger.warning(f"[{account_id}] Нет рабочих прокси для failover")

    async def set_proxy(self, account_id: str, proxy_url: Optional[str]) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        # Проверяем корректность URL до сохранения
        _parse_proxy(proxy_url)  # raises ValueError если формат неверный
        cfg.proxy = proxy_url
        self._save_configs()
        self._proxy_errors.pop(account_id, None)
        # Переподключаем клиент с новым прокси
        client = self.clients.get(account_id)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        await self._connect_client(account_id, cfg.phone)
        if proxy_url:
            _p = urlparse(proxy_url if "://" in proxy_url else "http://" + proxy_url)
            _qs = parse_qs(_p.query)
            proxy_host = (_qs.get("server") or _qs.get("host") or [None])[0] or _p.hostname
        else:
            proxy_host = None
        logger.info(f"[{account_id}] Прокси обновлён: {proxy_host or 'без прокси'}")
        return {"account_id": account_id, "proxy": proxy_url, "proxy_host": proxy_host}

    async def assign_all_proxies(self) -> dict:
        """
        Назначает прокси из пула всем аккаунтам.
        Фаза 1 (sync): выбирает совместимые прокси для каждого аккаунта.
        Фаза 2 (background): переподключает аккаунты в фоне — HTTP-ответ возвращается сразу.
        """
        if not self.proxy_pool:
            raise ValueError("Proxy pool не инициализирован")

        # Фаза 1: распределяем прокси (синхронно, чтобы правильно работала
        # логика наименее загруженного прокси)
        task_list: list[tuple[str, str]] = []   # (account_id, proxy_url)
        skipped: list[str] = []

        for account_id in list(self.configs.keys()):
            if not self.configs.get(account_id):
                continue

            tried: set = set()
            chosen = None
            while True:
                p = self.proxy_pool.get_best_proxy(exclude_ids=tried)
                if not p:
                    break
                try:
                    _parse_proxy(p["url"])
                    chosen = p
                    break
                except ValueError:
                    tried.add(p["id"])

            if not chosen:
                skipped.append(account_id)
                continue

            self.proxy_pool.assign_proxy_to_account(account_id, proxy_id=chosen["id"])
            task_list.append((account_id, chosen["url"]))

        # Фаза 2: запускаем переподключение в фоне и возвращаем ответ сразу
        sem = asyncio.Semaphore(5)

        async def _reconnect(account_id: str, proxy_url: str) -> None:
            async with sem:
                try:
                    await self.set_proxy(account_id, proxy_url)
                except Exception as e:
                    logger.error(f"[assign_all_proxies] [{account_id}] Ошибка: {e}")

        async def _run_all() -> None:
            await asyncio.gather(*[_reconnect(aid, url) for aid, url in task_list])
            logger.info(f"[assign_all_proxies] Завершено: {len(task_list)} аккаунтов")

        asyncio.create_task(_run_all())

        return {
            "started": len(task_list),
            "skipped": len(skipped),
        }

    def update_spam_ban_auto(self, account_id: str, enabled: bool) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        cfg.spam_ban_auto = enabled
        self._save_configs()
        return {"account_id": account_id, "spam_ban_auto": cfg.spam_ban_auto}

    def update_warmup_initiator(self, account_id: str, enabled: bool) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        cfg.warmup_initiator = enabled
        self._save_configs()
        return {"account_id": account_id, "warmup_initiator": cfg.warmup_initiator}

    def update_warmup_receiver(self, account_id: str, enabled: bool) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        cfg.warmup_receiver = enabled
        self._save_configs()
        return {"account_id": account_id, "warmup_receiver": cfg.warmup_receiver}

    def update_mailing_enabled(self, account_id: str, enabled: bool) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        cfg.mailing_enabled = enabled
        self._save_configs()
        return {"account_id": account_id, "mailing_enabled": cfg.mailing_enabled}

    def update_link_preview(self, account_id: str, link_preview_disabled: Optional[bool] = None) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if link_preview_disabled is not None:
            cfg.link_preview_disabled = link_preview_disabled
        self._save_configs()
        return {
            "account_id": account_id,
            "link_preview_disabled": cfg.link_preview_disabled,
        }

    # ------------------------------------------------------------------ #
    #  Спам-бан: ручное обновление даты снятия                            #
    # ------------------------------------------------------------------ #

    def set_spam_ban(self, account_id: str, banned_until) -> dict:
        cfg = self.configs.get(account_id)
        if cfg is None:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        cfg.banned_until = banned_until
        self._save_configs()
        return {
            "account_id": account_id,
            "banned_until": banned_until.isoformat() if banned_until else None,
        }

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
            path = self._sessions_dir / f"{account_id}{suffix}"
            if path.exists():
                path.unlink()

        # Чистим все словари
        self.clients.pop(account_id, None)
        self.configs.pop(account_id, None)
        self.authorized.pop(account_id, None)
        self._auth_state.pop(account_id, None)

        if self.proxy_pool:
            self.proxy_pool.unassign_account(account_id)
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

    async def get_dialogs(self, account_id: str, limit: int = 30, sent_only: bool = False) -> list[dict]:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        if not client.is_connected():
            raise ValueError(f"Аккаунт '{account_id}' не подключён к Telegram")
        sent_ids = self._sent_chats.get(account_id, set())
        # Для "Все диалоги" (sent_only=False) берём больше чтобы после фильтра
        # по User осталось достаточно — у пользователей может быть много групп
        fetch_limit = limit if sent_only else max(limit * 4, 400)
        try:
            dialogs = await client.get_dialogs(limit=fetch_limit)
        except Exception as e:
            raise ValueError(f"Ошибка получения диалогов: {e}") from e
        result = []
        for d in dialogs:
            entity = d.entity
            # В режиме "Все диалоги" показываем только личные чаты с пользователями
            if not sent_only and not isinstance(entity, User):
                continue
            if sent_only and d.id not in sent_ids:
                continue
            last_msg = d.message
            status = getattr(entity, "status", None)
            result.append({
                "id": d.id,
                "name": d.name or str(d.id),
                "username": getattr(entity, "username", None),
                "unread_count": d.unread_count,
                "last_online": self._parse_status(status) if status is not None else None,
                "last_message": {
                    "text": (last_msg.text or "")[:120],
                    "date": last_msg.date.isoformat(),
                    "out": last_msg.out,
                } if last_msg else None,
            })
        return result

    async def resolve_username(self, account_id: str, username: str) -> dict:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        clean = username.strip().lstrip("@")
        if not clean:
            raise ValueError("Username не указан")
        try:
            entity = await client.get_entity(clean)
        except Exception as e:
            raise ValueError(f"Пользователь @{clean} не найден: {e}") from e
        name = " ".join(filter(None, [
            getattr(entity, "first_name", "") or "",
            getattr(entity, "last_name", "") or "",
        ])).strip() or clean
        return {
            "id": entity.id,
            "name": name,
            "username": getattr(entity, "username", None) or clean,
        }

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

        # Получаем read_outbox_max_id — до какого исходящего собеседник прочитал
        read_outbox_max_id = 0
        try:
            from telethon.tl.functions.messages import GetPeerDialogsRequest
            from telethon.tl.types import InputDialogPeer
            peer_result = await client(GetPeerDialogsRequest(peers=[InputDialogPeer(entity)]))
            if peer_result.dialogs:
                read_outbox_max_id = peer_result.dialogs[0].read_outbox_max_id or 0
        except Exception:
            pass

        result = []
        for m in reversed(list(msgs)):
            result.append({
                "id": m.id,
                "text": m.text or "",
                "date": m.date.isoformat(),
                "out": m.out,
                "read": m.out and m.id <= read_outbox_max_id,
            })
        return result

    async def get_channel_last_post(self, account_id: str, channel: str) -> Optional[dict]:
        """Получить последний пост канала с текстом."""
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")

        client = self.clients[account_id]
        clean = channel.strip().lstrip("@")
        try:
            entity = await asyncio.wait_for(client.get_entity(clean), timeout=30)
        except Exception as e:
            raise ValueError(f"Не удалось резолвить канал @{clean}: {e}")

        msgs = await client.get_messages(entity, limit=10)

        for msg in msgs:
            if not msg.text:
                continue
            has_discussion = hasattr(msg, "replies") and msg.replies is not None
            return {
                "post_id": msg.id,
                "text": msg.text,
                "date": msg.date.isoformat(),
                "has_discussion": has_discussion,
            }
        return None

    async def send_comment(self, account_id: str, channel: str, post_id: int, text: str, instant: bool = False) -> dict:
        """Оставить комментарий к посту канала через discussion group."""
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")

        client = self.clients[account_id]
        clean = channel.strip().lstrip("@")

        try:
            channel_entity = await asyncio.wait_for(client.get_entity(clean), timeout=30)
        except Exception as e:
            raise ValueError(f"Не удалось резолвить канал @{clean}: {e}")

        from telethon.tl.functions.messages import GetDiscussionMessageRequest
        try:
            discussion = await asyncio.wait_for(
                client(GetDiscussionMessageRequest(peer=channel_entity, msg_id=post_id)),
                timeout=30,
            )
        except Exception as e:
            raise ValueError(f"Не удалось получить discussion для поста {post_id}: {e}")

        if not discussion.chats:
            raise ValueError(f"У канала @{clean} нет discussion group")

        group = discussion.chats[0]
        reply_to_id = discussion.messages[0].id if discussion.messages else post_id

        text = text.replace("—", "-").replace("–", "-")

        if not instant:
            cfg = self.configs.get(account_id)
            if cfg and cfg.typing_enabled:
                await asyncio.sleep(2)
                t_min = cfg.typing_min_seconds
                t_max = max(cfg.typing_max_seconds, t_min)
                typing_duration = random.uniform(t_min, t_max)
                try:
                    async with client.action(group, "typing"):
                        await asyncio.sleep(typing_duration)
                except Exception:
                    pass

        cfg = self.configs.get(account_id)
        link_preview = not (cfg.link_preview_disabled if cfg else False)
        await asyncio.wait_for(
            client.send_message(group, text, reply_to=reply_to_id, link_preview=link_preview),
            timeout=60,
        )

        return {"status": "commented", "account_id": account_id, "channel": clean, "post_id": post_id}

    async def mark_read(self, account_id: str, chat_id: int) -> None:
        if account_id not in self.clients:
            raise ValueError(f"Аккаунт '{account_id}' не найден")
        if not self.authorized.get(account_id):
            raise ValueError(f"Аккаунт '{account_id}' не авторизован")
        client = self.clients[account_id]
        try:
            entity = await client.get_entity(chat_id)
        except Exception:
            entity = chat_id
        await client.send_read_acknowledge(entity)

    def find_account_by_username(self, username: str) -> Optional[str]:
        """Найти account_id по Telegram @username аккаунта-отправителя."""
        clean = username.strip().lstrip("@").lower()
        for account_id, cfg in self.configs.items():
            if cfg.username and cfg.username.lower() == clean:
                return account_id
        return None

    def get_status(self) -> list[dict]:
        result = []
        now = datetime.now(timezone.utc)
        for account_id, cfg in self.configs.items():
            client = self.clients.get(account_id)
            authorized = self.authorized.get(account_id, False)
            banned_until = cfg.banned_until if cfg else None
            is_banned = banned_until is not None and banned_until > now
            result.append({
                "account_id": account_id,
                "tg_id": cfg.tg_id if cfg else None,
                "phone": cfg.phone if cfg else "—",
                "name": cfg.name if cfg else "—",
                "username": cfg.username if cfg else None,
                "connected": client.is_connected() if client else False,
                "authorized": authorized,
                "available": authorized and not is_banned,
                "banned_until": banned_until.isoformat() if banned_until else None,
                "pending_auth": account_id in self._auth_state,
                "typing_enabled": cfg.typing_enabled if cfg else True,
                "typing_min_seconds": cfg.typing_min_seconds if cfg else 7.0,
                "typing_max_seconds": cfg.typing_max_seconds if cfg else 10.0,
                "link_preview_disabled": cfg.link_preview_disabled if cfg else False,
                "spam_ban_auto": cfg.spam_ban_auto if cfg else True,
                "warmup_initiator": cfg.warmup_initiator if cfg else False,
                "warmup_receiver": cfg.warmup_receiver if cfg else False,
                "mailing_enabled": cfg.mailing_enabled if cfg else True,
                "proxy": cfg.proxy if cfg else None,
                "proxy_type": "mtproto" if cfg and cfg.proxy and ("t.me/proxy" in cfg.proxy or cfg.proxy.startswith("tg://proxy")) else ("socks" if cfg and cfg.proxy else None),
                "proxy_error": self._proxy_errors.get(account_id),
            })
        return result
