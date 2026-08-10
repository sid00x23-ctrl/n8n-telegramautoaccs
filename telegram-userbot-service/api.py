"""
FastAPI-приложение.
HTTP API для n8n и авторизации аккаунтов на лету.
"""
import logging
from fastapi import FastAPI, HTTPException
from typing import Optional

logger = logging.getLogger(__name__)

from manager import AccountManager, _parse_proxy
from models import SendMessageRequest, ReactRequest, AuthStartRequest, AuthCompleteRequest, EditMessageRequest, TypingSettingsRequest, LinkPreviewRequest, SpamBanRequest, SpamBanAutoRequest, WarmupInitiatorRequest, WarmupReceiverRequest, MailingRequest, ProxyRequest, AddProxyRequest, ProxySettingsRequest


def create_app(manager: AccountManager, proxy_pool=None, commenting_manager: Optional[AccountManager] = None) -> FastAPI:
    app = FastAPI(
        title="Telegram Userbot Service",
        description="HTTP API для n8n — отправка сообщений через личные Telegram-аккаунты",
        version="1.0.0",
        docs_url="/docs",
    )

    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "accounts": manager.get_status()}

    @app.get("/accounts", tags=["system"])
    async def list_accounts():
        return manager.get_status()

    @app.post("/send", tags=["messaging"])
    async def send_message(body: SendMessageRequest):
        """
        Отправить сообщение от имени аккаунта.
        Вызывается нодами n8n.

        Body: {"account_id": "account1", "chat_id": 123456789, "text": "Привет!"}

        Альтернативно:
        - partner_id: @username аккаунта-отправителя (вместо account_id)
        - lastsender_id: @username получателя (вместо username/chat_id для резолва entity)
        """
        try:
            # Определяем account_id: явный приоритет над partner_id
            account_id = body.account_id
            if not account_id:
                if not body.partner_id:
                    raise HTTPException(status_code=400, detail="Необходимо указать account_id или partner_id")
                clean_partner = body.partner_id.strip().lstrip("@")
                account_id = manager.find_account_by_username(clean_partner)
                if not account_id:
                    raise HTTPException(status_code=404, detail=f"Аккаунт с никнеймом @{clean_partner} не найден")

            # Определяем username для резолва получателя: lastsender_id имеет приоритет над username
            username = body.lastsender_id or body.username

            if not username and body.chat_id is None:
                raise HTTPException(status_code=400, detail="Необходимо указать chat_id или lastsender_id/username получателя")

            return await manager.send_message(account_id, body.chat_id, body.text, username, body.rate_limited, body.instant)
        except HTTPException:
            raise
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ------------------------------------------------------------------ #
    #  Авторизация через телефон (SMS / Telegram App)                      #
    # ------------------------------------------------------------------ #

    @app.post("/accounts/{account_id}/auth/start", tags=["auth"])
    async def auth_start(account_id: str, body: AuthStartRequest):
        """
        Шаг 1: запросить код подтверждения.
        Код придёт в приложение Telegram или по SMS.
        """
        result = await manager.start_authorization(account_id, body.phone)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/accounts/{account_id}/auth/resend", tags=["auth"])
    async def auth_resend(account_id: str):
        """
        Повторно запросить код другим методом (обычно SMS после App).
        """
        result = await manager.resend_authorization_code(account_id)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/accounts/{account_id}/auth/code", tags=["auth"])
    async def auth_complete(account_id: str, body: AuthCompleteRequest):
        """
        Шаг 2: ввести код (и пароль 2FA если нужно).
        Если аккаунт защищён 2FA и password не передан — вернёт status=2fa_required.
        """
        result = await manager.complete_authorization(account_id, body.code, body.password)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    # ------------------------------------------------------------------ #
    #  Авторизация через QR-код                                            #
    # ------------------------------------------------------------------ #

    @app.post("/accounts/{account_id}/auth/qr", tags=["auth"])
    async def auth_qr_start(account_id: str):
        """
        Начать QR-авторизацию. Возвращает URL для генерации QR-кода.
        Отсканируйте QR в приложении Telegram → Настройки → Устройства.
        """
        result = await manager.start_qr_authorization(account_id)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/accounts/{account_id}/auth/qr/refresh", tags=["auth"])
    async def auth_qr_refresh(account_id: str):
        """Обновить QR-код (если предыдущий истёк)."""
        result = await manager.refresh_qr_authorization(account_id)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.post("/accounts/{account_id}/auth/qr/wait", tags=["auth"])
    async def auth_qr_wait(account_id: str, password: Optional[str] = None):
        """
        Ждать сканирования QR (до 30 сек). Если status=timeout — вызови снова.
        Если аккаунт с 2FA и password не передан — вернёт status=2fa_required;
        тогда повтори запрос с ?password=...
        """
        result = await manager.wait_qr_authorization(account_id, password)
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result["message"])
        return result

    @app.get("/accounts/{account_id}/resolve/{username}", tags=["messaging"])
    async def resolve_username(account_id: str, username: str):
        try:
            return await manager.resolve_username(account_id, username)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/accounts/{account_id}/dialogs", tags=["messaging"])
    async def get_dialogs(account_id: str, limit: int = 30, sent_only: bool = False):
        try:
            return await manager.get_dialogs(account_id, limit, sent_only)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            logger.exception(f"[get_dialogs] {account_id}: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @app.get("/accounts/{account_id}/dialogs/{chat_id}/messages", tags=["messaging"])
    async def get_messages(account_id: str, chat_id: int, limit: int = 50, offset_id: int = 0):
        try:
            return await manager.get_messages(account_id, chat_id, limit, offset_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/accounts/{account_id}/dialogs/{chat_id}/read", tags=["messaging"])
    async def mark_read(account_id: str, chat_id: int):
        try:
            await manager.mark_read(account_id, chat_id)
            return {"ok": True}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}", tags=["messaging"])
    async def delete_message(account_id: str, chat_id: int, message_id: int):
        try:
            return await manager.delete_message(account_id, chat_id, message_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/accounts/{account_id}/dialogs/{chat_id}/messages/{message_id}", tags=["messaging"])
    async def edit_message(account_id: str, chat_id: int, message_id: int, body: EditMessageRequest):
        try:
            return await manager.edit_message(account_id, chat_id, message_id, body.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/accounts/{account_id}/typing", tags=["system"])
    async def update_typing(account_id: str, body: TypingSettingsRequest):
        """
        Обновить настройки typing для аккаунта.

        Body (все поля опциональны):
        - typing_enabled: true/false — включить/выключить индикатор набора
        - typing_min_seconds: минимальное время набора (секунды)
        - typing_max_seconds: максимальное время набора (секунды)
        """
        try:
            return manager.update_typing_settings(
                account_id,
                body.typing_enabled,
                body.typing_min_seconds,
                body.typing_max_seconds,
            )
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/spam_ban", tags=["system"])
    async def set_spam_ban(account_id: str, body: SpamBanRequest):
        """
        Обновить дату снятия спам-бана.
        Body: {"banned_until": "2026-07-23T15:05:00Z"} или {"banned_until": null} для снятия.
        """
        try:
            return manager.set_spam_ban(account_id, body.banned_until)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/proxy", tags=["system"])
    async def set_proxy(account_id: str, body: ProxyRequest):
        """
        Установить прокси для аккаунта. Аккаунт переподключится через новый прокси.

        Body: {"proxy": "socks5://user:pass@host:port"} или {"proxy": null} — без прокси.
        """
        try:
            return await manager.set_proxy(account_id, body.proxy)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.patch("/accounts/{account_id}/warmup_initiator", tags=["system"])
    async def update_warmup_initiator(account_id: str, body: WarmupInitiatorRequest):
        """
        Включить/выключить флаг инициатора прогрева для аккаунта.

        Body: {"enabled": true/false}
        """
        try:
            return manager.update_warmup_initiator(account_id, body.enabled)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/warmup_receiver", tags=["system"])
    async def update_warmup_receiver(account_id: str, body: WarmupReceiverRequest):
        """
        Включить/выключить флаг получателя прогрева для аккаунта.

        Body: {"enabled": true/false}
        """
        try:
            return manager.update_warmup_receiver(account_id, body.enabled)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/mailing", tags=["system"])
    async def update_mailing_enabled(account_id: str, body: MailingRequest):
        """
        Включить/выключить аккаунт из рассылки.

        Body: {"enabled": true/false}
        """
        try:
            return manager.update_mailing_enabled(account_id, body.enabled)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/spam_ban_auto", tags=["system"])
    async def update_spam_ban_auto(account_id: str, body: SpamBanAutoRequest):
        """
        Включить/выключить автоматическое выставление спам-бана при PeerFloodError.

        Body: {"enabled": true/false}
        """
        try:
            return manager.update_spam_ban_auto(account_id, body.enabled)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.patch("/accounts/{account_id}/link_preview", tags=["system"])
    async def update_link_preview(account_id: str, body: LinkPreviewRequest):
        """
        Обновить настройку превью ссылок для аккаунта.

        Body:
        - link_preview_disabled: true/false — отключить/включить превью ссылок
        """
        try:
            return manager.update_link_preview(account_id, body.link_preview_disabled)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.delete("/accounts/{account_id}", tags=["system"])
    async def delete_account(account_id: str):
        """
        Удалить аккаунт: выйти из Telegram, стереть сессию и конфиг.
        """
        if account_id not in manager.configs and account_id not in manager.clients:
            raise HTTPException(status_code=404, detail=f"Аккаунт '{account_id}' не найден")
        await manager.logout(account_id)
        return {"status": "deleted", "account_id": account_id}

    # ------------------------------------------------------------------ #
    #  Proxy pool                                                           #
    # ------------------------------------------------------------------ #

    @app.get("/proxies", tags=["proxies"])
    async def list_proxies():
        if not proxy_pool:
            return []
        proxies = proxy_pool.list_proxies()
        for p in proxies:
            try:
                _parse_proxy(p["url"])
                p["compatible"] = True
            except Exception:
                p["compatible"] = False
        return proxies

    @app.post("/proxies", tags=["proxies"])
    async def add_proxy(body: AddProxyRequest):
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        try:
            return await proxy_pool.add_proxy(body.url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/proxies/assign", tags=["proxies"])
    async def assign_proxies():
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        try:
            return await manager.assign_all_proxies()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/proxies/import", tags=["proxies"])
    async def import_proxies():
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        try:
            return await proxy_pool.import_from_toproxylab()
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/proxies/settings", tags=["proxies"])
    async def get_proxy_settings():
        if not proxy_pool:
            return {"check_interval_seconds": 300}
        return proxy_pool.get_settings()

    @app.patch("/proxies/settings", tags=["proxies"])
    async def update_proxy_settings(body: ProxySettingsRequest):
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        return proxy_pool.update_settings(body.check_interval_seconds)

    @app.post("/proxies/check-all", tags=["proxies"])
    async def check_all_proxies():
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        return await proxy_pool.check_all_proxies()

    @app.post("/proxies/{proxy_id}/check", tags=["proxies"])
    async def check_proxy(proxy_id: str):
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        try:
            return await proxy_pool.check_proxy(proxy_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.delete("/proxies/{proxy_id}", tags=["proxies"])
    async def remove_proxy(proxy_id: str):
        if not proxy_pool:
            raise HTTPException(status_code=503, detail="Proxy pool не инициализирован")
        try:
            return proxy_pool.remove_proxy(proxy_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.post("/react", tags=["messaging"])
    async def react_message(body: ReactRequest):
        """
        Поставить реакцию на сообщение (по умолчанию ❤).
        Body: {"account_id": "account1", "chat_id": 123456789, "message_id": 987}
        """
        try:
            return await manager.react_to_message(body.account_id, body.chat_id, body.message_id, body.emoji, body.username)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    # ------------------------------------------------------------------ #
    #  Commenting accounts                                                  #
    # ------------------------------------------------------------------ #

    if commenting_manager is not None:

        @app.get("/commenting/accounts", tags=["commenting"])
        async def commenting_list_accounts():
            return commenting_manager.get_status()

        @app.post("/commenting/accounts/{account_id}/auth/start", tags=["commenting"])
        async def commenting_auth_start(account_id: str, body: AuthStartRequest):
            result = await commenting_manager.start_authorization(account_id, body.phone)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.post("/commenting/accounts/{account_id}/auth/resend", tags=["commenting"])
        async def commenting_auth_resend(account_id: str):
            result = await commenting_manager.resend_authorization_code(account_id)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.post("/commenting/accounts/{account_id}/auth/code", tags=["commenting"])
        async def commenting_auth_code(account_id: str, body: AuthCompleteRequest):
            result = await commenting_manager.complete_authorization(account_id, body.code, body.password)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.post("/commenting/accounts/{account_id}/auth/qr", tags=["commenting"])
        async def commenting_auth_qr_start(account_id: str):
            result = await commenting_manager.start_qr_authorization(account_id)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.post("/commenting/accounts/{account_id}/auth/qr/refresh", tags=["commenting"])
        async def commenting_auth_qr_refresh(account_id: str):
            result = await commenting_manager.refresh_qr_authorization(account_id)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.post("/commenting/accounts/{account_id}/auth/qr/wait", tags=["commenting"])
        async def commenting_auth_qr_wait(account_id: str, password: Optional[str] = None):
            result = await commenting_manager.wait_qr_authorization(account_id, password)
            if result.get("status") == "error":
                raise HTTPException(status_code=400, detail=result["message"])
            return result

        @app.delete("/commenting/accounts/{account_id}", tags=["commenting"])
        async def commenting_delete_account(account_id: str):
            if account_id not in commenting_manager.configs and account_id not in commenting_manager.clients:
                raise HTTPException(status_code=404, detail=f"Аккаунт '{account_id}' не найден")
            await commenting_manager.logout(account_id)
            return {"status": "deleted", "account_id": account_id}

    return app
