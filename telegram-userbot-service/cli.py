"""
Интерактивный CLI для управления аккаунтами — до и во время работы сервиса.
"""
import asyncio
import io
import sys
from typing import TYPE_CHECKING

import qrcode
import questionary
from questionary import Style
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box
from rich.align import Align

if TYPE_CHECKING:
    from manager import AccountManager

console = Console()

# Стиль questionary — немного кастомизируем
STYLE = Style(
    [
        ("qmark", "fg:#00bcd4 bold"),
        ("question", "bold"),
        ("answer", "fg:#00e676 bold"),
        ("pointer", "fg:#00bcd4 bold"),
        ("highlighted", "fg:#00bcd4 bold"),
        ("selected", "fg:#00e676"),
        ("separator", "fg:#555555"),
        ("instruction", "fg:#888888"),
    ]
)


class CLI:
    def __init__(self, manager: "AccountManager"):
        self.manager = manager

    # ------------------------------------------------------------------ #
    #  Главный цикл                                                        #
    # ------------------------------------------------------------------ #

    async def run(self) -> bool:
        """
        Запускает интерактивное меню.
        Возвращает True если нужно запустить сервис, False — выход.
        """
        self._print_header()

        while True:
            self._print_accounts_table()

            choice = await self._main_menu()

            if choice is None or choice == "exit":
                console.print("\n[dim]До свидания.[/dim]")
                return False

            elif choice == "continue":
                return True

            elif choice == "auth_existing":
                await self._flow_authorize_existing()

            elif choice == "auth_new":
                await self._flow_authorize_new()

            elif choice == "auth_qr":
                await self._flow_qr_authorize()

            elif choice == "logout_one":
                await self._flow_logout_one()

            elif choice == "logout_all":
                await self._flow_logout_all()

            # После любого действия (кроме continue/exit) — очищаем и показываем снова

    # ------------------------------------------------------------------ #
    #  Отображение                                                          #
    # ------------------------------------------------------------------ #

    def _print_header(self):
        console.print()
        console.print(Panel(
            Text("Telegram Userbot Service", justify="center", style="bold cyan"),
            border_style="cyan",
            padding=(0, 4),
        ))
        console.print()

    def _print_accounts_table(self):
        statuses = self.manager.get_status()

        console.print("[bold]Авторизованные аккаунты:[/bold]")
        console.print()

        if not statuses:
            console.print("  [dim]Нет добавленных аккаунтов.[/dim]\n")
            return

        table = Table(box=box.ROUNDED, show_header=True, header_style="bold cyan", padding=(0, 1))
        table.add_column("ID",         style="bold",  min_width=10)
        table.add_column("Имя",                       min_width=14)
        table.add_column("Username",   style="dim",   min_width=14)
        table.add_column("Телефон",                   min_width=14)
        table.add_column("Telegram ID", style="dim",  min_width=12)
        table.add_column("Статус",                    min_width=16)

        for s in statuses:
            if s["authorized"]:
                status_str = "[green]✓ активен[/green]"
            elif s["pending_auth"]:
                status_str = "[yellow]⏳ ожидает кода[/yellow]"
            else:
                status_str = "[red]✗ не авторизован[/red]"

            username = f"@{s['username']}" if s["username"] else "—"
            tg_id = str(s["tg_id"]) if s["tg_id"] else "—"

            table.add_row(
                s["account_id"],
                s["name"] or "—",
                username,
                s["phone"],
                tg_id,
                status_str,
            )

        console.print(table)
        console.print()

    # ------------------------------------------------------------------ #
    #  Главное меню                                                         #
    # ------------------------------------------------------------------ #

    async def _main_menu(self) -> str | None:
        statuses = self.manager.get_status()
        has_authorized = any(s["authorized"] for s in statuses)
        has_unauthorized = any(not s["authorized"] and not s["pending_auth"] for s in statuses)

        choices = []

        if has_authorized:
            choices.append(questionary.Choice("▶  Продолжить (запустить сервис)", value="continue"))

        if has_unauthorized:
            choices.append(questionary.Choice("🔑 Авторизовать добавленные аккаунты", value="auth_existing"))

        choices.append(questionary.Choice("➕ Добавить через номер телефона", value="auth_new"))
        choices.append(questionary.Choice("📱 Добавить через QR-код", value="auth_qr"))

        if has_authorized:
            choices.append(questionary.Choice("🚪 Выйти из аккаунта", value="logout_one"))
            choices.append(questionary.Choice("🗑  Выйти из всех аккаунтов", value="logout_all"))

        choices.append(questionary.Choice("✕  Выход", value="exit"))

        return await questionary.select(
            "Выберите действие:",
            choices=choices,
            style=STYLE,
        ).ask_async()

    # ------------------------------------------------------------------ #
    #  Авторизация добавленных (существующих) аккаунтов                    #
    # ------------------------------------------------------------------ #

    async def _flow_authorize_existing(self):
        console.print()
        console.rule("[cyan]Авторизация добавленных аккаунтов[/cyan]")
        console.print()

        statuses = self.manager.get_status()
        unauthorized = [s for s in statuses if not s["authorized"] and not s["pending_auth"]]

        if not unauthorized:
            console.print("[yellow]Все аккаунты уже авторизованы.[/yellow]\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()
            return

        choices = [
            questionary.Choice(
                f"{s['account_id']}  —  {s['phone']}",
                value=s["account_id"],
            )
            for s in unauthorized
        ]

        selected_ids = await questionary.checkbox(
            "Отметьте аккаунты для авторизации (Пробел — выбор, Enter — подтвердить):",
            choices=choices,
            style=STYLE,
        ).ask_async()

        if not selected_ids:
            console.print("[dim]Ничего не выбрано.[/dim]\n")
            return

        console.print(f"\n[dim]Выбрано аккаунтов: {len(selected_ids)}[/dim]\n")

        for idx, account_id in enumerate(selected_ids, 1):
            cfg = self.manager.configs.get(account_id)
            phone = cfg.phone

            console.print()
            console.rule(f"[cyan]({idx}/{len(selected_ids)}) {account_id}  —  {phone}[/cyan]")
            console.print()

            console.print(f"[dim]Отправляем код на {phone}...[/dim]")
            result = await self.manager.start_authorization(account_id, phone)

            if result["status"] != "code_sent":
                console.print(f"[red]Ошибка:[/red] {result['message']}\n")
                if idx < len(selected_ids):
                    cont = await questionary.confirm(
                        "Продолжить со следующим аккаунтом?",
                        default=True,
                        style=STYLE,
                    ).ask_async()
                    if not cont:
                        break
                continue

            self._show_code_sent(result)

            code = await self._prompt_code(account_id)
            if not code:
                continue

            auth_result = await self._complete_auth(account_id, code)
            if auth_result is None:
                continue

            if auth_result["status"] == "authorized":
                name = auth_result.get("name", "")
                username = auth_result.get("username", "")
                uname_str = f" (@{username})" if username else ""
                console.print(f"\n[green]✓ Авторизован:[/green] {name}{uname_str}\n")
            else:
                console.print(f"\n[red]Ошибка:[/red] {auth_result.get('message', 'Неизвестная ошибка')}\n")
                if idx < len(selected_ids):
                    cont = await questionary.confirm(
                        "Продолжить со следующим аккаунтом?",
                        default=True,
                        style=STYLE,
                    ).ask_async()
                    if not cont:
                        break

        console.print()
        await questionary.press_any_key_to_continue(style=STYLE).ask_async()

    # ------------------------------------------------------------------ #
    #  Авторизация нового аккаунта                                          #
    # ------------------------------------------------------------------ #

    async def _flow_authorize_new(self):
        console.print()
        console.rule("[cyan]Авторизация нового аккаунта[/cyan]")
        console.print()

        # Предлагаем следующий свободный ID
        existing = set(self.manager.clients.keys())
        suggested_id = next(
            f"account{i}" for i in range(1, 100) if f"account{i}" not in existing
        )

        account_id = await questionary.text(
            "ID аккаунта (латиница, без пробелов):",
            default=suggested_id,
            validate=lambda v: (
                True if v.strip() and v.strip().replace("_", "").isalnum()
                else "Только буквы, цифры и _"
            ),
            style=STYLE,
        ).ask_async()

        if not account_id:
            return
        account_id = account_id.strip()

        phone = await questionary.text(
            "Номер телефона (формат: +79001234567):",
            validate=lambda v: (
                True if v.strip().startswith("+") and v.strip()[1:].isdigit() and len(v.strip()) >= 10
                else "Введите номер в формате +7XXXXXXXXXX"
            ),
            style=STYLE,
        ).ask_async()

        if not phone:
            return
        phone = phone.strip()

        console.print(f"\n[dim]Отправляем код на {phone}...[/dim]")
        result = await self.manager.start_authorization(account_id, phone)

        if result["status"] != "code_sent":
            console.print(f"\n[red]Ошибка:[/red] {result['message']}\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()
            return

        self._show_code_sent(result)

        code = await self._prompt_code(account_id)
        if not code:
            return

        auth_result = await self._complete_auth(account_id, code)
        if auth_result is None:
            return

        if auth_result["status"] == "authorized":
            name = auth_result.get("name", "")
            username = auth_result.get("username", "")
            uname_str = f" (@{username})" if username else ""
            console.print(f"\n[green]✓ Аккаунт авторизован:[/green] {name}{uname_str}\n")
        else:
            console.print(f"\n[red]Ошибка:[/red] {auth_result.get('message', 'Неизвестная ошибка')}\n")

        await questionary.press_any_key_to_continue(style=STYLE).ask_async()

    # ------------------------------------------------------------------ #
    #  Общие helpers для ввода кода                                         #
    # ------------------------------------------------------------------ #

    def _show_code_sent(self, result: dict):
        code_via = result.get("code_via", "")
        console.print(f"[green]✓[/green] Код отправлен [bold]{code_via}[/bold]")
        if result.get("code_type") == "SentCodeTypeApp":
            console.print("[dim]  Откройте Telegram на телефоне с этим номером — сообщение от сервисного аккаунта «Telegram»[/dim]")
        elif result.get("code_type") == "SentCodeTypeSms":
            console.print("[dim]  Проверьте SMS на указанном номере[/dim]")
        console.print()

    async def _prompt_code(self, account_id: str | None = None) -> str | None:
        choices = [
            questionary.Choice("Ввести код", value="enter"),
            questionary.Choice("Переотправить код другим способом", value="resend"),
            questionary.Choice("Отмена", value="cancel"),
        ]

        action = await questionary.select(
            "Код получен?",
            choices=choices,
            style=STYLE,
        ).ask_async()

        if action == "cancel" or action is None:
            return None

        if action == "resend" and account_id:
            console.print("\n[dim]Запрашиваем переотправку...[/dim]")
            resend_result = await self.manager.resend_authorization_code(account_id)
            if resend_result["status"] == "code_sent":
                self._show_code_sent(resend_result)
            else:
                console.print(f"[red]Ошибка переотправки:[/red] {resend_result.get('message', '')}\n")

        code = await questionary.text(
            "Введите код из Telegram:",
            validate=lambda v: True if v.strip().isdigit() else "Только цифры",
            style=STYLE,
        ).ask_async()
        return code.strip() if code else None

    async def _complete_auth(self, account_id: str, code: str) -> dict | None:
        console.print("\n[dim]Проверяем код...[/dim]")
        result = await self.manager.complete_authorization(account_id, code)

        if result["status"] == "2fa_required":
            console.print("[yellow]Аккаунт защищён двухфакторной аутентификацией.[/yellow]\n")
            password = await questionary.password(
                "Введите пароль 2FA:",
                style=STYLE,
            ).ask_async()
            if not password:
                return None
            console.print("\n[dim]Проверяем пароль...[/dim]")
            result = await self.manager.complete_authorization(account_id, code, password)

        return result

    # ------------------------------------------------------------------ #
    #  QR-авторизация                                                       #
    # ------------------------------------------------------------------ #

    def _display_qr(self, url: str):
        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        f = io.StringIO()
        qr.print_ascii(out=f, invert=True)
        qr_text = f.getvalue()
        console.print(Align.center(Panel(
            qr_text.strip(),
            title="[cyan]Отсканируй в Telegram[/cyan]",
            border_style="cyan",
            padding=(0, 2),
        )))
        console.print(Align.center("[dim]Settings → Devices → Scan QR Code[/dim]\n"))

    async def _flow_qr_authorize(self):
        console.print()
        console.rule("[cyan]Авторизация через QR-код[/cyan]")
        console.print()

        existing = set(self.manager.clients.keys())
        suggested_id = next(
            f"account{i}" for i in range(1, 100) if f"account{i}" not in existing
        )

        account_id = await questionary.text(
            "ID аккаунта (латиница, без пробелов):",
            default=suggested_id,
            validate=lambda v: (
                True if v.strip() and v.strip().replace("_", "").isalnum()
                else "Только буквы, цифры и _"
            ),
            style=STYLE,
        ).ask_async()

        if not account_id:
            return
        account_id = account_id.strip()

        console.print("\n[dim]Получаем QR-код...[/dim]")
        result = await self.manager.start_qr_authorization(account_id)

        if result["status"] != "qr_ready":
            console.print(f"\n[red]Ошибка:[/red] {result['message']}\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()
            return

        self._display_qr(result["url"])

        # Цикл ожидания сканирования (по 30 сек, с обновлением QR)
        while True:
            wait_result = await self.manager.wait_qr_authorization(account_id)

            if wait_result["status"] == "authorized":
                name = wait_result.get("name", "")
                username = wait_result.get("username", "")
                uname_str = f" (@{username})" if username else ""
                console.print(f"\n[green]✓ Авторизован:[/green] {name}{uname_str}\n")
                break

            elif wait_result["status"] == "timeout":
                console.print("[yellow]QR-код истёк. Обновляем...[/yellow]")
                refresh = await self.manager.refresh_qr_authorization(account_id)
                if refresh["status"] == "qr_ready":
                    self._display_qr(refresh["url"])
                    continue
                else:
                    console.print(f"[red]Ошибка обновления:[/red] {refresh.get('message', '')}\n")
                    break

            elif wait_result["status"] == "2fa_required":
                console.print("[yellow]Аккаунт защищён двухфакторной аутентификацией.[/yellow]\n")
                password = await questionary.password(
                    "Введите пароль 2FA:",
                    style=STYLE,
                ).ask_async()
                if not password:
                    break
                fa_result = await self.manager.wait_qr_authorization(account_id, password)
                if fa_result["status"] == "authorized":
                    name = fa_result.get("name", "")
                    username = fa_result.get("username", "")
                    uname_str = f" (@{username})" if username else ""
                    console.print(f"\n[green]✓ Авторизован:[/green] {name}{uname_str}\n")
                else:
                    console.print(f"\n[red]Ошибка:[/red] {fa_result.get('message', '')}\n")
                break

            else:
                console.print(f"\n[red]Ошибка:[/red] {wait_result.get('message', '')}\n")
                break

        await questionary.press_any_key_to_continue(style=STYLE).ask_async()

    # ------------------------------------------------------------------ #
    #  Выход из одного аккаунта                                             #
    # ------------------------------------------------------------------ #

    async def _flow_logout_one(self):
        console.print()
        console.rule("[cyan]Выйти из аккаунта[/cyan]")
        console.print()

        statuses = self.manager.get_status()
        authorized = [s for s in statuses if s["authorized"]]

        if not authorized:
            console.print("[yellow]Нет авторизованных аккаунтов.[/yellow]\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()
            return

        choices = [
            questionary.Choice(
                f"{s['account_id']}  —  {s['name'] or ''}  {s['phone']}",
                value=s["account_id"],
            )
            for s in authorized
        ]
        choices.append(questionary.Choice("← Назад", value=None))

        account_id = await questionary.select(
            "Из какого аккаунта выйти?",
            choices=choices,
            style=STYLE,
        ).ask_async()

        if not account_id:
            return

        confirmed = await questionary.confirm(
            f"Выйти из аккаунта '{account_id}'? Сессия будет удалена.",
            default=False,
            style=STYLE,
        ).ask_async()

        if confirmed:
            await self.manager.logout(account_id)
            console.print(f"\n[green]✓[/green] Аккаунт [bold]{account_id}[/bold] удалён.\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()

    # ------------------------------------------------------------------ #
    #  CLI во время работы сервиса                                         #
    # ------------------------------------------------------------------ #

    async def run_live(self, log_handler=None):
        """
        Живой CLI: сервис работает в фоне, логи идут в консоль.
        Нажатие Enter открывает меню управления аккаунтами.
        Возвращает управление когда пользователь выбирает «Остановить сервис».
        """
        loop = asyncio.get_event_loop()
        console.print("[dim]Нажмите Enter для меню управления аккаунтами...[/dim]\n")

        while True:
            try:
                await loop.run_in_executor(None, sys.stdin.readline)
            except (EOFError, OSError, asyncio.CancelledError):
                break

            if log_handler:
                log_handler.set_cli_active(True)
            try:
                should_continue = await self._live_menu()
            finally:
                if log_handler:
                    log_handler.set_cli_active(False)

            if not should_continue:
                break

            console.print("[dim]Нажмите Enter для меню управления аккаунтами...[/dim]\n")

    async def _live_menu(self) -> bool:
        """
        Меню управления аккаунтами пока сервис запущен.
        Возвращает False если нужно остановить сервис.
        """
        while True:
            self._print_accounts_table()
            choice = await self._live_main_menu_choices()

            if choice is None or choice == "back":
                return True

            elif choice == "stop":
                confirmed = await questionary.confirm(
                    "Остановить сервис?",
                    default=False,
                    style=STYLE,
                ).ask_async()
                if confirmed:
                    return False
                # не подтвердил — остаёмся в меню

            elif choice == "auth_new":
                await self._flow_authorize_new()

            elif choice == "auth_qr":
                await self._flow_qr_authorize()

            elif choice == "logout_one":
                await self._flow_logout_one()

            elif choice == "logout_all":
                await self._flow_logout_all()

    async def _live_main_menu_choices(self) -> str | None:
        statuses = self.manager.get_status()
        has_authorized = any(s["authorized"] for s in statuses)

        choices = [
            questionary.Choice("← Вернуться к логам", value="back"),
            questionary.Choice("➕ Добавить аккаунт (телефон)", value="auth_new"),
            questionary.Choice("📱 Добавить аккаунт (QR-код)", value="auth_qr"),
        ]

        if has_authorized:
            choices.append(questionary.Choice("🚪 Выйти из аккаунта", value="logout_one"))
            choices.append(questionary.Choice("🗑  Выйти из всех аккаунтов", value="logout_all"))

        choices.append(questionary.Choice("⏹  Остановить сервис", value="stop"))

        return await questionary.select(
            "Управление аккаунтами:",
            choices=choices,
            style=STYLE,
        ).ask_async()

    # ------------------------------------------------------------------ #
    #  Выход из всех аккаунтов                                              #
    # ------------------------------------------------------------------ #

    async def _flow_logout_all(self):
        console.print()
        console.rule("[red]Выйти из всех аккаунтов[/red]")
        console.print()

        count = len([s for s in self.manager.get_status() if s["authorized"]])

        confirmed = await questionary.confirm(
            f"Выйти из всех {count} аккаунтов? Все сессии будут удалены.",
            default=False,
            style=STYLE,
        ).ask_async()

        if confirmed:
            await self.manager.logout_all()
            console.print(f"\n[green]✓[/green] Все аккаунты удалены.\n")
            await questionary.press_any_key_to_continue(style=STYLE).ask_async()
