from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    # Получить на https://my.telegram.org/apps
    TELEGRAM_API_ID: int
    TELEGRAM_API_HASH: str

    # Куда слать входящие сообщения (n8n Webhook)
    N8N_WEBHOOK_URL: str = "http://localhost:5678/webhook/telegram-incoming"

    # Куда слать результаты отправки (успех / ошибка)
    N8N_DELIVERY_WEBHOOK_URL: str = "http://localhost:5678/webhook/telegram-delivery"

    # Сервис
    SERVICE_HOST: str = "0.0.0.0"
    SERVICE_PORT: int = 8000
    SESSIONS_DIR: Path = Path("sessions")

    model_config = {"env_file": ".env"}


settings = Settings()
settings.SESSIONS_DIR.mkdir(exist_ok=True)
