import asyncio
import logging
import os
import sys
import threading

import uvicorn
from rich.console import Console

from api import create_app
from cli import CLI
from config import settings
from manager import AccountManager
from proxy_manager import ProxyPool
from commenting_channels import ChannelManager

console = Console()

HEADLESS = os.environ.get("HEADLESS", "").lower() in ("1", "true", "yes")

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class CLIAwareHandler(logging.StreamHandler):
    """
    Обработчик логов, который буферизует вывод пока открыто CLI-меню,
    чтобы questionary-промпты не перемешивались со строками логов.
    """

    def __init__(self):
        super().__init__(sys.stderr)
        self.setFormatter(logging.Formatter(LOG_FORMAT))
        self._cli_active = False
        self._buffer: list[logging.LogRecord] = []
        self._lock = threading.Lock()

    def set_cli_active(self, active: bool):
        with self._lock:
            self._cli_active = active
            if not active and self._buffer:
                for record in self._buffer:
                    try:
                        super().emit(record)
                    except Exception:
                        pass
                self._buffer.clear()

    def emit(self, record: logging.LogRecord):
        with self._lock:
            if self._cli_active:
                self._buffer.append(record)
            else:
                super().emit(record)


async def main():
    logging.basicConfig(level=logging.WARNING, format=LOG_FORMAT)
    logging.getLogger("telethon").setLevel(logging.ERROR)

    from pathlib import Path

    proxy_pool = ProxyPool()
    manager = AccountManager(proxy_pool=proxy_pool)
    commenting_manager = AccountManager(
        configs_file=Path("commenting_accounts_config.json"),
        sessions_dir=Path("commenting_sessions"),
        sent_chats_file=Path("commenting_sent_chats.json"),
    )
    channel_manager = ChannelManager(
        channels_file=Path("commenting_channels.json"),
    )

    if HEADLESS:
        # Серверный режим: сначала поднимаем HTTP-сервер, потом подключаем аккаунты фоном.
        # Так порт 8000 появляется сразу, даже если какие-то аккаунты долго коннектятся.
        logging.getLogger().setLevel(logging.INFO)
        logging.getLogger("telethon").setLevel(logging.WARNING)

        app = create_app(manager, proxy_pool, commenting_manager, channel_manager)
        config = uvicorn.Config(app, host=settings.SERVICE_HOST, port=settings.SERVICE_PORT,
                                log_level="warning", access_log=False)
        server = uvicorn.Server(config)

        async def _start_accounts():
            await asyncio.gather(
                manager.start_all(),
                commenting_manager.start_all(),
            )
            statuses = manager.get_status()
            authorized = [s for s in statuses if s["authorized"]]
            comm_statuses = commenting_manager.get_status()
            comm_authorized = [s for s in comm_statuses if s["authorized"]]
            if not authorized and not comm_authorized:
                console.print("[yellow]HEADLESS: нет авторизованных аккаунтов.[/yellow]")
            else:
                console.print(f"[green]HEADLESS: рассылка={len(authorized)}, комментинг={len(comm_authorized)}[/green]")

        asyncio.create_task(_start_accounts())

        try:
            await server.serve()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            console.print("\n[dim]Завершение работы...[/dim]")
            await asyncio.gather(manager.stop_all(), commenting_manager.stop_all())
        return

    # Интерактивный режим: подключаем аккаунты синхронно, потом CLI
    await asyncio.gather(manager.start_all(), commenting_manager.start_all())
    cli = CLI(manager)
    should_start = await cli.run()

    if not should_start:
        await manager.stop_all()
        sys.exit(0)

    # Переключаем логирование на CLI-aware handler
    handler = CLIAwareHandler()
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("telethon").setLevel(logging.WARNING)

    # Запускаем HTTP-сервис фоновой задачей
    console.print(
        f"\n[bold green]Сервис запущен[/bold green]  "
        f"[dim]http://{settings.SERVICE_HOST}:{settings.SERVICE_PORT}[/dim]"
    )

    app = create_app(manager, proxy_pool, commenting_manager, channel_manager)
    config = uvicorn.Config(app, host=settings.SERVICE_HOST, port=settings.SERVICE_PORT,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    server_task = asyncio.create_task(server.serve())

    # CLI работает параллельно с сервисом
    try:
        await cli.run_live(handler)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        console.print("\n[dim]Завершение работы...[/dim]")
        server.should_exit = True
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            server_task.cancel()
        await asyncio.gather(manager.stop_all(), commenting_manager.stop_all())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
