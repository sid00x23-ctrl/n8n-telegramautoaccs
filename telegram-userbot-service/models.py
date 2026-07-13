from datetime import datetime
from pydantic import BaseModel
from typing import Optional


class SendMessageRequest(BaseModel):
    account_id: str
    chat_id: int          # числовой Telegram ID получателя
    text: str
    username: Optional[str] = None  # @username для первичного резолва entity
    rate_limited: bool = False      # если True — ждёт глобальную очередь (1 сообщение/мин)
    instant: bool = False           # если True — пропускает паузу и typing, шлёт сразу


class AuthStartRequest(BaseModel):
    phone: str            # формат: +79001234567


class AuthCompleteRequest(BaseModel):
    code: str             # код из SMS/Telegram
    password: Optional[str] = None  # пароль 2FA (если включён)


class EditMessageRequest(BaseModel):
    text: str


class ReactRequest(BaseModel):
    account_id: str
    chat_id: int
    message_id: int
    emoji: str = '❤'
    username: Optional[str] = None  # @username для резолва entity (как в send)


# Структура аккаунта в accounts_config.json
class AccountConfig(BaseModel):
    account_id: str             # уникальный ID, совпадает с executor_bot_id в Google Sheets
    phone: str
    tg_id: Optional[int] = None         # числовой Telegram user ID (заполняется после авторизации)
    name: Optional[str] = None          # имя из Telegram
    username: Optional[str] = None      # @username из Telegram
    banned_until: Optional[datetime] = None  # UTC datetime спам-бана; None = не забанен
    typing_enabled: bool = True         # показывать ли индикатор набора перед отправкой
    typing_min_seconds: float = 7.0     # минимальное время набора (секунд)
    typing_max_seconds: float = 10.0    # максимальное время набора (секунд)
    link_preview_disabled: bool = False # отключить превью ссылок в исходящих сообщениях


class TypingSettingsRequest(BaseModel):
    typing_enabled: Optional[bool] = None
    typing_min_seconds: Optional[float] = None
    typing_max_seconds: Optional[float] = None


class LinkPreviewRequest(BaseModel):
    link_preview_disabled: Optional[bool] = None
