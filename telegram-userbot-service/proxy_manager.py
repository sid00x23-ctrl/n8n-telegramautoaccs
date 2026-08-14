"""
Менеджер прокси-пула.
Хранит прокси в proxies.json, проверяет их TCP-коннектом,
назначает аккаунтам (наименее загруженный), делает failover при падении.
"""
import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, parse_qs

import httpx

logger = logging.getLogger(__name__)

PROXIES_FILE = Path("proxies.json")

_DEFAULT_SETTINGS: dict = {
    "check_interval_seconds": 300,
}


class ProxyPool:
    def __init__(self):
        self.proxies: list[dict] = []
        self.settings: dict = dict(_DEFAULT_SETTINGS)
        self._monitor_task: Optional[asyncio.Task] = None
        self._reconnect_callback = None  # async (account_id, new_proxy_url) -> None
        self._load()

    def set_reconnect_callback(self, callback):
        """Регистрирует callback для переподключения аккаунта на новый прокси."""
        self._reconnect_callback = callback

    # ------------------------------------------------------------------ #
    #  Персистентность                                                      #
    # ------------------------------------------------------------------ #

    def _load(self):
        if PROXIES_FILE.exists():
            data = json.loads(PROXIES_FILE.read_text())
            self.proxies = data.get("proxies", [])
            self.settings = {**_DEFAULT_SETTINGS, **data.get("settings", {})}
            logger.info(f"Загружено {len(self.proxies)} прокси")

    def _save(self):
        data = {"settings": self.settings, "proxies": self.proxies}
        PROXIES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))

    # ------------------------------------------------------------------ #
    #  Парсинг / определение типа                                           #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_host_port(url: str) -> Optional[tuple]:
        """Извлекает (host, port) из URL прокси для TCP-проверки."""
        try:
            url = url.strip()
            if "t.me/proxy" in url or url.startswith("tg://proxy"):
                if "://" not in url:
                    url = "https://" + url
                p = urlparse(url)
                qs = parse_qs(p.query)
                server = (qs.get("server") or qs.get("host") or [""])[0].strip()
                port = int((qs.get("port") or ["0"])[0].strip())
                if server and port:
                    return server, port
            else:
                if "://" not in url:
                    url = "http://" + url
                p = urlparse(url)
                if p.hostname and p.port:
                    return p.hostname, p.port
        except Exception:
            pass
        return None

    @staticmethod
    def _detect_type(url: str) -> str:
        u = url.lower()
        if "t.me/proxy" in u or u.startswith("tg://proxy"):
            return "mtproto"
        if "socks5://" in u:
            return "socks5"
        if "socks4://" in u:
            return "socks4"
        return "http"

    @staticmethod
    def _get_mtproto_secret(url: str) -> Optional[str]:
        """Извлекает секрет из MTProto URL."""
        try:
            url = url.strip()
            if "://" not in url:
                url = "https://" + url
            qs = parse_qs(urlparse(url).query)
            secret = (qs.get("secret") or [""])[0].strip()
            return secret or None
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    #  Проверка живости                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_fake_tls_secret(raw_secret: str) -> bool:
        """True если декодированный первый байт секрета == 0xEE (Fake-TLS)."""
        try:
            secret_bytes = bytes.fromhex(raw_secret)
        except ValueError:
            try:
                import base64
                secret_bytes = base64.urlsafe_b64decode(raw_secret + "==")
            except Exception:
                return False
        return len(secret_bytes) > 0 and secret_bytes[0] == 0xEE

    async def _check_alive(self, url: str, timeout: float = 10.0) -> Optional[str]:
        """Проверяет прокси. Для MTProto ee-прокси — полный Fake-TLS хэндшейк с ключом."""
        hp = self._get_host_port(url)
        if not hp:
            return "Не удалось распарсить адрес прокси"
        host, port = hp

        # Для MTProto прокси — Fake-TLS хэндшейк + реальный Telegram-тест
        if self._detect_type(url) == "mtproto":
            secret = self._get_mtproto_secret(url)
            if secret and self._is_fake_tls_secret(secret):
                # Шаг 1: Fake-TLS хэндшейк
                tls_error = await self._check_alive_fake_tls(host, port, secret, timeout)
                if tls_error:
                    return tls_error
                # Шаг 2: реальное Telegram-соединение через прокси
                return await self._check_alive_telegram(url, timeout=20.0)

        # Для остальных — TCP-коннект
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return None
        except asyncio.TimeoutError:
            return f"Таймаут ({host}:{port})"
        except OSError as e:
            return f"Недоступен ({host}:{port}): {e.strerror}"
        except Exception as e:
            return str(e)

    async def _check_alive_fake_tls(
        self, host: str, port: int, raw_secret: str, timeout: float = 10.0
    ) -> Optional[str]:
        """
        Проверяет MTProto ee-прокси через полный Fake-TLS хэндшейк.
        Повторяет логику ConnectionTcpMTProxyFakeTLS._connect(), поэтому
        статус 'ok' означает что и хост доступен, и ключ принят сервером.
        """
        from fake_tls_mtproxy import _do_fake_tls_handshake, _extract_domain_from_secret

        # Шаг 1: TCP-подключение
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
        except asyncio.TimeoutError:
            return f"Таймаут ({host}:{port})"
        except OSError as e:
            return f"Недоступен ({host}:{port}): {e.strerror}"
        except Exception as e:
            return str(e)

        try:
            # Шаг 2: декодируем секрет — повторяем Telethon normalize_secret
            try:
                secret_bytes = bytes.fromhex(raw_secret)
            except ValueError:
                import base64
                secret_bytes = base64.urlsafe_b64decode(raw_secret + "==")

            # Повторяем логику TcpMTProxy._connect():
            # если первый байт — 0xEE/0xDD и длина > 16 — убираем байт-префикс
            if len(secret_bytes) > 16 and secret_bytes[0] in (0xDD, 0xEE):
                hmac_key = secret_bytes[1:]
            else:
                hmac_key = secret_bytes

            domain = _extract_domain_from_secret(raw_secret) or "www.google.com"

            # Шаг 3: Fake-TLS хэндшейк (ClientHello → ServerHello → CCS → AppData)
            await asyncio.wait_for(
                _do_fake_tls_handshake(reader, writer, hmac_key, domain),
                timeout=timeout,
            )
            return None  # хэндшейк прошёл — прокси живой и ключ принят
        except ConnectionError as e:
            return f"Ошибка Fake-TLS хэндшейка: {e}"
        except asyncio.TimeoutError:
            return f"Таймаут Fake-TLS хэндшейка ({host}:{port})"
        except Exception as e:
            return f"Ошибка проверки MTProto ключа: {e}"
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _check_alive_telegram(self, url: str, timeout: float = 20.0) -> Optional[str]:
        """
        Реальная проверка прокси через TelegramClient.connect().
        Только так убеждаемся что MTProto трафик проходит до Telegram,
        а не просто TLS-хэндшейк.
        """
        import tempfile
        import os
        try:
            from manager import _parse_proxy
            from config import settings as _settings
        except Exception:
            return None  # не можем проверить — считаем ок

        try:
            proxy, connection_class = _parse_proxy(url)
        except Exception as e:
            return f"Ошибка парсинга прокси: {e}"

        tmp = tempfile.NamedTemporaryFile(suffix=".session", delete=False, dir="/tmp")
        tmp.close()
        try:
            from telethon import TelegramClient
            from telethon.network.connection import ConnectionTcpAbridged

            kwargs = {}
            if proxy:
                kwargs["proxy"] = proxy
            kwargs["connection"] = connection_class if connection_class else ConnectionTcpAbridged

            client = TelegramClient(
                tmp.name,
                _settings.TELEGRAM_API_ID,
                _settings.TELEGRAM_API_HASH,
                connection_retries=1,
                retry_delay=1,
                **kwargs,
            )
            try:
                await asyncio.wait_for(client.connect(), timeout=timeout)
                if not client.is_connected():
                    return "Telegram не ответил через прокси"
                return None
            except asyncio.TimeoutError:
                return f"Таймаут Telegram-соединения через прокси ({timeout:.0f}с)"
            except Exception as e:
                return f"Ошибка Telegram-соединения: {e}"
            finally:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        finally:
            for suffix in ["", "-journal"]:
                try:
                    os.unlink(tmp.name + suffix)
                except Exception:
                    pass

    # ------------------------------------------------------------------ #
    #  CRUD                                                                 #
    # ------------------------------------------------------------------ #

    async def add_proxy(self, url: str) -> dict:
        """Добавляет прокси в пул и сразу проверяет его."""
        url = url.strip()
        if not self._get_host_port(url):
            raise ValueError("Не удалось распарсить адрес прокси — проверьте формат URL")
        if any(p["url"] == url for p in self.proxies):
            raise ValueError("Этот прокси уже добавлен")

        proxy: dict = {
            "id": str(uuid.uuid4()),
            "url": url,
            "type": self._detect_type(url),
            "status": "checking",
            "error": None,
            "last_checked": None,
            "assigned_accounts": [],
        }
        self.proxies.append(proxy)
        self._save()

        error = await self._check_alive(url)
        proxy["status"] = "error" if error else "ok"
        proxy["error"] = error
        proxy["last_checked"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return proxy

    def remove_proxy(self, proxy_id: str) -> dict:
        proxy = next((p for p in self.proxies if p["id"] == proxy_id), None)
        if not proxy:
            raise ValueError(f"Прокси '{proxy_id}' не найден")
        self.proxies = [p for p in self.proxies if p["id"] != proxy_id]
        self._save()
        return proxy

    def list_proxies(self) -> list:
        return list(self.proxies)

    def get_settings(self) -> dict:
        return dict(self.settings)

    # ------------------------------------------------------------------ #
    #  Назначение аккаунтам                                                 #
    # ------------------------------------------------------------------ #

    def get_best_proxy(self, exclude_ids: Optional[set] = None) -> Optional[dict]:
        """Рабочий прокси с наименьшим числом назначенных аккаунтов."""
        exclude_ids = exclude_ids or set()
        working = [
            p for p in self.proxies
            if p["status"] == "ok" and p["id"] not in exclude_ids
        ]
        if not working:
            return None
        return min(working, key=lambda p: len(p.get("assigned_accounts", [])))

    def get_account_proxy(self, account_id: str) -> Optional[dict]:
        for p in self.proxies:
            if account_id in p.get("assigned_accounts", []):
                return p
        return None

    def assign_proxy_to_account(
        self,
        account_id: str,
        proxy_id: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Назначает прокси аккаунту, убирая его из предыдущего.
        proxy_id=None → выбирает лучший автоматически.
        """
        # Убираем из предыдущего прокси
        for p in self.proxies:
            lst = p.get("assigned_accounts", [])
            if account_id in lst:
                lst.remove(account_id)

        if proxy_id:
            proxy = next((p for p in self.proxies if p["id"] == proxy_id), None)
        else:
            proxy = self.get_best_proxy()

        if proxy:
            proxy.setdefault("assigned_accounts", [])
            if account_id not in proxy["assigned_accounts"]:
                proxy["assigned_accounts"].append(account_id)
            self._save()
        return proxy

    def unassign_account(self, account_id: str):
        """Убирает аккаунт из всех прокси."""
        changed = False
        for p in self.proxies:
            lst = p.get("assigned_accounts", [])
            if account_id in lst:
                lst.remove(account_id)
                changed = True
        if changed:
            self._save()

    # ------------------------------------------------------------------ #
    #  Failover                                                             #
    # ------------------------------------------------------------------ #

    async def failover(self, account_id: str, failed_proxy_id: str) -> Optional[dict]:
        """
        Помечает упавший прокси как error, назначает аккаунту следующий рабочий.
        Возвращает новый прокси или None если нет свободных.
        """
        failed = next((p for p in self.proxies if p["id"] == failed_proxy_id), None)
        if failed:
            failed["status"] = "error"
            failed["error"] = "Потеря соединения (failover)"
            failed["last_checked"] = datetime.now(timezone.utc).isoformat()
            lst = failed.get("assigned_accounts", [])
            if account_id in lst:
                lst.remove(account_id)
            self._save()

        new_proxy = self.get_best_proxy(exclude_ids={failed_proxy_id})
        if new_proxy:
            new_proxy.setdefault("assigned_accounts", [])
            if account_id not in new_proxy["assigned_accounts"]:
                new_proxy["assigned_accounts"].append(account_id)
            self._save()
            logger.info(
                f"[{account_id}] Failover: {failed_proxy_id[:8]}... → {new_proxy['id'][:8]}..."
            )
        else:
            logger.warning(f"[{account_id}] Failover: нет свободных рабочих прокси")
        return new_proxy

    # ------------------------------------------------------------------ #
    #  Проверка одного прокси                                              #
    # ------------------------------------------------------------------ #

    async def check_all_proxies(self) -> dict:
        """Проверяет все прокси параллельно. Возвращает итоги."""
        if not self.proxies:
            return {"total": 0, "ok": 0, "error": 0}
        tasks = [self._check_one_safe(p["id"]) for p in list(self.proxies)]
        await asyncio.gather(*tasks)
        ok = sum(1 for p in self.proxies if p["status"] == "ok")
        return {"total": len(self.proxies), "ok": ok, "error": len(self.proxies) - ok}

    async def check_proxy(self, proxy_id: str) -> dict:
        proxy = next((p for p in self.proxies if p["id"] == proxy_id), None)
        if not proxy:
            raise ValueError(f"Прокси '{proxy_id}' не найден")
        error = await self._check_alive(proxy["url"])
        proxy["status"] = "error" if error else "ok"
        proxy["error"] = error
        proxy["last_checked"] = datetime.now(timezone.utc).isoformat()
        self._save()
        return proxy

    # ------------------------------------------------------------------ #
    #  Фоновый мониторинг                                                   #
    # ------------------------------------------------------------------ #

    async def _rebalance(self):
        """
        Балансирует нагрузку: перераспределяет аккаунты по рабочим прокси
        так, чтобы разница между самым загруженным и самым незагруженным
        не превышала 1 аккаунта. Переподключает перемещённые аккаунты.
        """
        ok = [p for p in self.proxies if p["status"] == "ok"]
        if len(ok) < 2:
            return

        total = sum(len(p.get("assigned_accounts", [])) for p in ok)
        if total == 0:
            return

        target_max = (total + len(ok) - 1) // len(ok)  # ceil(total / n)

        moved_any = False
        # Итерируемся пока есть перегруженные
        for _ in range(total):
            overloaded = [p for p in ok if len(p.get("assigned_accounts", [])) > target_max]
            if not overloaded:
                break
            underloaded = sorted(
                [p for p in ok if len(p.get("assigned_accounts", [])) < target_max],
                key=lambda p: len(p.get("assigned_accounts", [])),
            )
            if not underloaded:
                break

            over = overloaded[0]
            under = underloaded[0]
            account_id = over["assigned_accounts"][0]

            over["assigned_accounts"].remove(account_id)
            under.setdefault("assigned_accounts", [])
            if account_id not in under["assigned_accounts"]:
                under["assigned_accounts"].append(account_id)
            moved_any = True

            if self._reconnect_callback:
                try:
                    await self._reconnect_callback(account_id, under["url"])
                    logger.info(
                        f"[rebalance] {account_id}: "
                        f"{over['id'][:8]}... → {under['id'][:8]}..."
                    )
                except Exception as e:
                    logger.error(f"[rebalance] {account_id}: ошибка переподключения: {e}")

        if moved_any:
            self._save()
            logger.info("[rebalance] Балансировка завершена")

    async def _monitor_loop(self):
        logger.info("Proxy monitor запущен")
        while True:
            interval = self.settings.get("check_interval_seconds", 300)
            await asyncio.sleep(interval)
            if not self.proxies:
                continue
            logger.info(f"[proxy_monitor] Проверка {len(self.proxies)} прокси...")
            tasks = [self._check_one_safe(p["id"]) for p in list(self.proxies)]
            await asyncio.gather(*tasks)
            await self._rebalance()
            logger.info("[proxy_monitor] Готово")

    async def _check_one_safe(self, proxy_id: str):
        proxy = next((p for p in self.proxies if p["id"] == proxy_id), None)
        if not proxy:
            return
        was_ok = proxy.get("status") == "ok"
        accounts_snapshot = list(proxy.get("assigned_accounts", []))
        try:
            await self.check_proxy(proxy_id)
        except Exception as e:
            logger.error(f"[proxy_monitor] Ошибка при проверке {proxy_id}: {e}")
            return

        now_error = proxy.get("status") == "error"
        if was_ok and now_error and accounts_snapshot and self._reconnect_callback:
            logger.warning(
                f"[proxy_monitor] Прокси {proxy_id[:8]}... упал, "
                f"переключаем {len(accounts_snapshot)} аккаунт(ов)..."
            )
            for account_id in accounts_snapshot:
                new_proxy = self.get_best_proxy(exclude_ids={proxy_id})
                if new_proxy:
                    self.assign_proxy_to_account(account_id, proxy_id=new_proxy["id"])
                    try:
                        await self._reconnect_callback(account_id, new_proxy["url"])
                        logger.info(
                            f"[proxy_monitor] {account_id}: "
                            f"{proxy_id[:8]}... → {new_proxy['id'][:8]}..."
                        )
                    except Exception as e:
                        logger.error(f"[proxy_monitor] {account_id}: ошибка переключения: {e}")
                else:
                    logger.warning(f"[proxy_monitor] {account_id}: нет рабочих прокси для переключения")

    async def start_monitor(self):
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def stop_monitor(self):
        if self._monitor_task:
            self._monitor_task.cancel()
            self._monitor_task = None

    # ------------------------------------------------------------------ #
    #  Настройки                                                            #
    # ------------------------------------------------------------------ #

    def update_settings(self, check_interval_seconds: Optional[int] = None) -> dict:
        if check_interval_seconds is not None:
            self.settings["check_interval_seconds"] = check_interval_seconds
        self._save()
        return self.settings

    # ------------------------------------------------------------------ #
    #  Импорт с toproxylab.com                                             #
    # ------------------------------------------------------------------ #

    async def import_from_toproxylab(self) -> dict:
        """
        Парсит MTProto прокси с toproxylab.com/ru/proksi-dlya-tg.
        Ищет ссылки tg://proxy?... и https://t.me/proxy?...
        """
        import html as html_module

        import_url = "https://toproxylab.com/ru/proksi-dlya-tg"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        try:
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers=headers,
            ) as client:
                resp = await client.get(import_url)
                resp.raise_for_status()
                # Разэкранируем HTML-entities (&amp; → &) перед поиском
                raw_html = html_module.unescape(resp.text)
        except Exception as e:
            raise ValueError(f"Не удалось загрузить страницу: {e}")

        # Пробуем вытащить все прокси из JS-переменной allMT (содержит все 50)
        import json as _json
        found_urls: list = []
        m = re.search(r"var allMT=(\[.*?\]);", raw_html, re.DOTALL)
        if m:
            try:
                all_mt = _json.loads(m.group(1))
                for item in all_mt:
                    link = item.get("link", "")
                    if link and link not in found_urls:
                        found_urls.append(link)
                logger.info(f"[toproxylab] Найдено {len(found_urls)} прокси из allMT")
            except Exception as e:
                logger.warning(f"[toproxylab] Не удалось разобрать allMT: {e}")

        # Fallback: regex по HTML если allMT не найден
        if not found_urls:
            pattern = r'(?:tg://proxy\?[^\s"\'<>\)\\]+|https?://t\.me/proxy\?[^\s"\'<>\)\\]+)'
            found_raw = re.findall(pattern, raw_html, re.IGNORECASE)
            found_urls = list(dict.fromkeys(found_raw))

        added: list = []
        skipped: list = []
        errors: list = []

        for proxy_url in found_urls:
            if any(p["url"] == proxy_url for p in self.proxies):
                skipped.append(proxy_url)
                continue
            try:
                proxy = await self.add_proxy(proxy_url)
                added.append(proxy)
            except Exception as e:
                errors.append({"url": proxy_url, "error": str(e)})

        if not found_urls:
            raise ValueError(
                "Прокси на странице не найдены — сайт мог изменить структуру."
            )

        return {
            "added": len(added),
            "skipped": len(skipped),
            "errors": len(errors),
            "proxies": added,
        }
