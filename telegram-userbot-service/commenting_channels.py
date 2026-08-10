"""
Хранилище каналов/групп для сервиса комментинга.
Простой JSON-файл: список записей с id, link, title, type, chat_id.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_FILE = Path("commenting_channels.json")


def _normalize_link(link: str) -> str:
    """Нормализует ссылку к виду https://t.me/..."""
    link = link.strip()
    if link.startswith("@"):
        return f"https://t.me/{link[1:]}"
    if link.startswith("t.me/"):
        return f"https://{link}"
    if not link.startswith("http"):
        return f"https://t.me/{link}"
    return link


class ChannelManager:
    def __init__(self, channels_file: Optional[Path] = None):
        self._file = channels_file or DEFAULT_FILE
        self._channels: dict[str, dict] = {}
        self._load()

    def _load(self):
        if self._file.exists():
            data = json.loads(self._file.read_text(encoding="utf-8"))
            self._channels = {ch["id"]: ch for ch in data}

    def _save(self):
        data = list(self._channels.values())
        self._file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def list_channels(self) -> list[dict]:
        return list(self._channels.values())

    def add_channel(self, link: str) -> dict:
        normalized = _normalize_link(link)

        # Проверка дублей по ссылке
        for ch in self._channels.values():
            if ch["link"] == normalized:
                raise ValueError(f"Канал уже добавлен: {normalized}")

        channel = {
            "id": str(uuid.uuid4()),
            "link": normalized,
            "title": None,
            "type": None,   # "channel" / "group" — заполняется при резолве
            "chat_id": None,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
        self._channels[channel["id"]] = channel
        self._save()
        return channel

    def remove_channel(self, channel_id: str) -> dict:
        if channel_id not in self._channels:
            raise ValueError(f"Канал не найден: {channel_id}")
        channel = self._channels.pop(channel_id)
        self._save()
        return channel

    def update_channel(self, channel_id: str, **kwargs) -> dict:
        """Обновить поля канала (title, type, chat_id)."""
        if channel_id not in self._channels:
            raise ValueError(f"Канал не найден: {channel_id}")
        allowed = {"title", "type", "chat_id"}
        for key, value in kwargs.items():
            if key in allowed:
                self._channels[channel_id][key] = value
        self._save()
        return self._channels[channel_id]
