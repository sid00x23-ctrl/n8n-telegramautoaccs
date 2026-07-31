from datetime import datetime
from pydantic import BaseModel
from typing import Optional


class SendMessageRequest(BaseModel):
    account_id: Optional[str] = None  # внутренний ID аккаунта-отправителя (или partner_id для поиска по никнейму)
    chat_id: Optional[int] = None     # числовой Telegram ID получателя (не нужен если указан lastsender_id/@username)
    text: str
    username: Optional[str] = None    # @username для первичного резолва entity получателя
    rate_limited: bool = False         # если True — проверяет спам-бан перед отправкой
    instant: bool = False             # если True — пропускает паузу и typing, шлёт сразу
    partner_id: Optional[str] = None  # @username аккаунта-отправителя (альтернатива account_id)
    lastsender_id: Optional[str] = None  # @username получателя (альтернатива username + chat_id резолв)


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
    spam_ban_auto: bool = True          # автоматически выставлять спам-бан при PeerFloodError
    warmup_initiator: bool = False      # аккаунт инициирует прогревочные диалоги
    warmup_receiver: bool = False       # аккаунт может быть выбран инициатором как получатель
    proxy: Optional[str] = None         # прокси URL: socks5://user:pass@host:port или http://...


class TypingSettingsRequest(BaseModel):
    typing_enabled: Optional[bool] = None
    typing_min_seconds: Optional[float] = None
    typing_max_seconds: Optional[float] = None


class LinkPreviewRequest(BaseModel):
    link_preview_disabled: Optional[bool] = None


class SpamBanRequest(BaseModel):
    banned_until: Optional[datetime] = None  # UTC datetime; None — снять бан


class SpamBanAutoRequest(BaseModel):
    enabled: bool  # True — автоматически ставить бан при PeerFloodError, False — не ставить


class WarmupInitiatorRequest(BaseModel):
    enabled: bool  # True — аккаунт инициирует прогревочные диалоги


class WarmupReceiverRequest(BaseModel):
    enabled: bool  # True — аккаунт может быть выбран инициатором как получатель


class ProxyRequest(BaseModel):
    proxy: Optional[str] = None  # socks5://user:pass@host:port | http://... | None — без прокси


class AddProxyRequest(BaseModel):
    url: str  # tg://proxy?... | socks5://... | http://...


class ProxySettingsRequest(BaseModel):
    check_interval_seconds: Optional[int] = None
