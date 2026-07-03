"""
FastAPI-приложение.
HTTP API для n8n и авторизации аккаунтов на лету.
"""
from fastapi import FastAPI, HTTPException
from typing import Optional

from manager import AccountManager
from models import SendMessageRequest, ReactRequest, AuthStartRequest, AuthCompleteRequest


def create_app(manager: AccountManager) -> FastAPI:
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
        """
        try:
            return await manager.send_message(body.account_id, body.chat_id, body.text, body.username, body.rate_limited, body.instant)
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

    @app.get("/accounts/{account_id}/dialogs", tags=["messaging"])
    async def get_dialogs(account_id: str, limit: int = 30, only_sent: bool = False):
        try:
            return await manager.get_dialogs(account_id, limit, only_sent)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/accounts/{account_id}/dialogs/{chat_id}/messages", tags=["messaging"])
    async def get_messages(account_id: str, chat_id: int, limit: int = 50, offset_id: int = 0):
        try:
            return await manager.get_messages(account_id, chat_id, limit, offset_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.delete("/accounts/{account_id}", tags=["system"])
    async def delete_account(account_id: str):
        """
        Удалить аккаунт: выйти из Telegram, стереть сессию и конфиг.
        """
        if account_id not in manager.configs and account_id not in manager.clients:
            raise HTTPException(status_code=404, detail=f"Аккаунт '{account_id}' не найден")
        await manager.logout(account_id)
        return {"status": "deleted", "account_id": account_id}

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

    return app
