#!/usr/bin/env python3
"""
Интерактивное управление Telegram-аккаунтами (Telethon).

Запуск без аргументов:

    ./tg_manager.py

Дальше всё делается в терминале через меню: список аккаунтов, добавление,
рассылка, вступление/выход из чатов, инфо, удаление, настройки.
Все файлы сессий лежат в папке sessions/ рядом со скриптом.
api_id / api_hash берутся из config.json (спрашиваются при первом запуске)
или из переменных окружения TELEGRAM_API_ID / TELEGRAM_API_HASH.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
from pathlib import Path

try:
    from telethon import TelegramClient, errors, functions
except ImportError:  # pragma: no cover
    sys.stderr.write("Не установлен Telethon. Выполните:\n  pip install -r requirements.txt\n")
    raise SystemExit(2)

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "sessions"
CONFIG_PATH = BASE_DIR / "config.json"

WIDTH = 66
INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)"
)

IS_TTY = sys.stdout.isatty()
USE_COLOR = IS_TTY and os.getenv("NO_COLOR") is None
_paint_codes = {
    "red": "31",
    "green": "32",
    "yellow": "33",
    "cyan": "36",
    "bold": "1",
    "dim": "2",
}


def paint(text: str, *styles: str) -> str:
    if not USE_COLOR or not styles:
        return text
    codes = ";".join(_paint_codes[s] for s in styles if s in _paint_codes)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def clear_screen() -> None:
    if IS_TTY:
        sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()


def line(char: str = "─") -> str:
    return paint(char * WIDTH, "cyan")


# --------------------------------------------------------------------------- #
# Ввод и вывод
# --------------------------------------------------------------------------- #
class Cancelled(Exception):
    """Пользователь прервал ввод (Ctrl+C / Ctrl+D)."""


def ask(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{paint(prompt, 'bold')}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    return value or (default or "")


def ask_secret(prompt: str) -> str:
    if not IS_TTY:  # getpass не умеет без терминала — обычный ввод
        return ask(prompt)
    try:
        return getpass.getpass(f"{paint(prompt, 'bold')}: ").strip()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None


def confirm(prompt: str, default: bool = False) -> bool:
    hint = " [Y/n]" if default else " [y/N]"
    try:
        value = input(f"{paint(prompt, 'bold')}{hint}: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None
    if not value:
        return default
    return value in {"y", "yes", "д", "да"}


def pause(text: str = "Enter — продолжить") -> None:
    try:
        input(paint(f"\n{text} ", "dim"))
    except (EOFError, KeyboardInterrupt):
        raise Cancelled from None


def draw_header(subtitle: str | None = None) -> None:
    clear_screen()
    count = len(session_files())
    print(line("═"))
    print(f"  {paint('TG Manager', 'bold', 'cyan')}   аккаунтов в sessions/: {count}")
    if subtitle:
        print(f"  {paint(subtitle, 'dim')}")
    print(line("═"))
    print()


def ok(text: str) -> str:
    return paint("✓", "green") + " " + text


def fail(text: str) -> str:
    return paint("✗", "red") + " " + text


def warn(text: str) -> str:
    return paint("!", "yellow") + " " + text


# --------------------------------------------------------------------------- #
# Конфиг, сессии, прокси
# --------------------------------------------------------------------------- #
def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except (OSError, ValueError):
            pass
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def parse_proxy(value: str | None):
    """socks5://user:pass@host:port | socks5://host:port | host:port"""
    if not value:
        return None
    raw = value.strip()
    scheme = "socks5"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
    scheme = scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        raise ValueError(f"неизвестная схема прокси: {scheme} (socks5, socks4, http)")
    auth = None
    if "@" in raw:
        auth, raw = raw.rsplit("@", 1)
    if ":" not in raw:
        raise ValueError("прокси задаётся как host:port или scheme://user:pass@host:port")
    host, port = raw.rsplit(":", 1)
    proxy = {"proxy_type": scheme, "addr": host, "port": int(port), "rdns": True}
    if auth:
        user, _, password = auth.partition(":")
        proxy["username"] = user
        proxy["password"] = password
    return proxy


def safe_session_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.+-]", "_", (name or "").strip())
    if not cleaned or cleaned in {".", ".."}:
        raise ValueError("недопустимое имя сессии")
    return cleaned


def session_files() -> list[tuple[str, Path]]:
    if not SESSION_DIR.is_dir():
        return []
    return [(p.stem, p) for p in sorted(SESSION_DIR.glob("*.session")) if p.is_file()]


def account_label(me) -> str:
    parts = []
    name = " ".join(x for x in [getattr(me, "first_name", None), getattr(me, "last_name", None)] if x)
    if name:
        parts.append(name)
    if getattr(me, "username", None):
        parts.append("@" + me.username)
    if getattr(me, "phone", None):
        parts.append("+" + str(me.phone))
    parts.append(f"id={me.id}")
    return " | ".join(parts)


def make_client(api_id: int, api_hash: str, path: Path, proxy) -> TelegramClient:
    return TelegramClient(
        str(path.with_suffix("")),
        api_id,
        api_hash,
        proxy=proxy,
        device_model="TG Manager",
        system_version="1.0",
        app_version="1.0",
        lang_code="ru",
        system_lang_code="ru",
    )


def normalize_target(target: str):
    raw = (target or "").strip()
    if not raw:
        raise ValueError("пустая цель")
    if INVITE_RE.search(raw):
        return None  # приглашение: сначала join
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^(t\.me|telegram\.me|telegram\.dog)/", "", raw)
    raw = re.sub(r"^s/", "", raw)
    raw = raw.split("?")[0].strip("/").lstrip("@")
    if not raw:
        raise ValueError("пустая цель")
    if INVITE_RE.search(raw) or raw.startswith("+") or raw.startswith("joinchat/"):
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


async def resolve_entity(client, target: str):
    parsed = normalize_target(target)
    if parsed is None:
        raise ValueError("это ссылка-приглашение: сначала вступите через «Вступить в чат»")
    return await client.get_entity(parsed)


def human_error(exc: Exception) -> str:
    if isinstance(exc, errors.FloodWaitError):
        return f"FloodWait: подождать {exc.seconds} с"
    if isinstance(exc, errors.SessionPasswordNeededError):
        return "нужен пароль 2FA"
    if isinstance(exc, errors.AuthKeyUnregisteredError):
        return "сессия отозвана, нужен повторный вход"
    if isinstance(exc, errors.UserDeactivatedBanError):
        return "аккаунт заблокирован"
    if isinstance(exc, errors.ChatWriteForbiddenError):
        return "нет права писать в этот чат"
    if isinstance(exc, errors.ChatAdminRequiredError):
        return "нужны права администратора"
    if isinstance(exc, errors.ChannelPrivateError):
        return "канал/чат приватный или аккаунт не участник"
    if isinstance(exc, errors.InviteHashExpiredError):
        return "ссылка-приглашение истекла"
    if isinstance(exc, errors.InviteHashInvalidError):
        return "ссылка-приглашение недействительна"
    return f"{type(exc).__name__}: {exc}"


def parse_selection(raw: str, count: int) -> list[int] | None:
    raw = (raw or "").strip().lower()
    if raw in {"all", "a", "все", "*"}:
        return list(range(1, count + 1))
    picked: set[int] = set()
    for token in re.split(r"[,\s]+", raw):
        if not token:
            continue
        span = re.fullmatch(r"(\d+)\s*-\s*(\d+)", token)
        if span:
            lo, hi = sorted((int(span.group(1)), int(span.group(2))))
            picked.update(range(lo, hi + 1))
        elif token.isdigit():
            picked.add(int(token))
        else:
            return None
    if not picked or any(i < 1 or i > count for i in picked):
        return None
    return sorted(picked)


# --------------------------------------------------------------------------- #
# Асинхронные операции
# --------------------------------------------------------------------------- #
async def run_for_accounts(
    accounts: list[tuple[str, Path]],
    api_id: int,
    api_hash: str,
    proxy,
    concurrency: int,
    delay: float,
    handler,
) -> int:
    """Прогоняет handler(client, name) по всем аккаунтам. Возвращает число ошибок."""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    lock = asyncio.Lock()
    total = len(accounts)
    state = {"done": 0, "failed": 0}

    async def worker(name: str, path: Path) -> None:
        async with semaphore:
            if delay:
                await asyncio.sleep(delay)
            client = None
            try:
                client = make_client(api_id, api_hash, path, proxy)
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("сессия не авторизована, выполните вход заново")
                result = await handler(client, name)
                async with lock:
                    state["done"] += 1
                    counter = f"({state['done'] + state['failed']}/{total})"
                    print(
                        f"  {paint('[ok]  ', 'green')} {name}: {result or 'готово'}"
                        f"  {paint(counter, 'dim')}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 — отчёт по каждому аккаунту
                async with lock:
                    state["failed"] += 1
                    counter = f"({state['done'] + state['failed']}/{total})"
                    print(
                        f"  {paint('[fail]', 'red')} {name}: {human_error(exc)}"
                        f"  {paint(counter, 'dim')}",
                        flush=True,
                    )
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

    await asyncio.gather(*(worker(name, path) for name, path in accounts))
    return state["failed"]


async def fetch_status(
    accounts: list[tuple[str, Path]], api_id: int, api_hash: str, proxy
) -> dict[str, tuple[bool, str]]:
    """{имя: (авторизована ли, описание)}"""
    statuses: dict[str, tuple[bool, str]] = {}
    semaphore = asyncio.Semaphore(5)

    async def one(name: str, path: Path) -> None:
        client = None
        async with semaphore:
            try:
                client = make_client(api_id, api_hash, path, proxy)
                await client.connect()
                if not await client.is_user_authorized():
                    statuses[name] = (False, "не авторизована — выполните вход заново")
                    return
                me = await client.get_me()
                bits = []
                if getattr(me, "username", None):
                    bits.append("@" + me.username)
                if getattr(me, "phone", None):
                    bits.append("+" + str(me.phone))
                bits.append(f"id={me.id}")
                statuses[name] = (True, " · ".join(bits))
            except Exception as exc:  # noqa: BLE001
                statuses[name] = (False, human_error(exc))
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass

    await asyncio.gather(*(one(name, path) for name, path in accounts))
    return statuses


# --------------------------------------------------------------------------- #
# Приложение
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, overrides: dict) -> None:
        self.overrides = overrides
        self.cfg = load_config()
        self.status: dict[str, tuple[bool, str]] = {}
        self.must_exit = False

    # ---------- служебное ----------
    def concurrency(self) -> int:
        try:
            return max(1, int(self.cfg.get("concurrency") or 3))
        except (TypeError, ValueError):
            return 3

    def delay(self) -> float:
        try:
            return max(0.0, float(self.cfg.get("delay") or 0))
        except (TypeError, ValueError):
            return 0.0

    def proxy(self):
        raw = self.overrides.get("proxy") or self.cfg.get("proxy")
        try:
            return parse_proxy(raw)
        except ValueError as exc:
            print(fail(str(exc)))
            return None

    def api(self, prompt_if_missing: bool = True):
        api_id = self.overrides.get("api_id") or os.getenv("TELEGRAM_API_ID") or self.cfg.get("api_id")
        api_hash = self.overrides.get("api_hash") or os.getenv("TELEGRAM_API_HASH") or self.cfg.get("api_hash")
        if not api_id or not api_hash:
            if not prompt_if_missing:
                return None
            draw_header("Первый запуск: API-доступ")
            print("api_id и api_hash бесплатно выдаются на https://my.telegram.org")
            print("→ API development tools. Введите их один раз — сохраню в config.json.\n")
            api_id = ask("api_id")
            api_hash = ask_secret("api_hash")
            if not re.fullmatch(r"\d+", api_id or "") or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or ""):
                print(fail("Некорректные api_id/api_hash"))
                pause()
                return None
            self.cfg["api_id"] = int(api_id)
            self.cfg["api_hash"] = api_hash
            save_config(self.cfg)
            print(ok(f"Сохранено в {CONFIG_PATH.name}"))
        try:
            return int(api_id), str(api_hash)
        except (TypeError, ValueError):
            print(fail("api_id должен быть числом"))
            return None

    def show(self, message: str) -> None:
        print(message)
        pause()

    def select_accounts(self, many: bool = True) -> list[tuple[str, Path]]:
        """Меню выбора аккаунтов по номерам. Пустой список — отмена."""
        sessions = session_files()
        if not sessions:
            print(warn(f"в {SESSION_DIR} нет сессий — сначала добавьте аккаунт"))
            pause()
            return []

        while True:
            draw_header("Выбор аккаунтов" if many else "Выбор аккаунта")
            for index, (name, path) in enumerate(sessions, 1):
                entry = self.status.get(name)
                state_text = paint(entry[1], "dim") if entry else paint("статус не запрошен", "dim")
                size_kb = f"{path.stat().st_size / 1024:.0f} КБ"
                print(f"  {index:>2}) {name:<16} {state_text}  {paint(size_kb, 'dim')}")
            print()
            if not many:
                raw = ask("Номер аккаунта (Enter — отмена)")
                if not raw:
                    return []
                if not raw.isdigit() or not 1 <= int(raw) <= len(sessions):
                    print(warn("нет такого номера"))
                    pause()
                    continue
                return [sessions[int(raw) - 1]]

            raw = ask("Номера (1,3 или 1-3), all, Enter — отмена")
            if not raw:
                return []
            picked = parse_selection(raw, len(sessions))
            if picked is None:
                print(warn("не понял выбор, попробуйте снова"))
                pause()
                continue
            return [sessions[i - 1] for i in picked]

    def run_bulk(self, accounts, handler, title: str) -> None:
        api = self.api()
        if api is None:
            return
        draw_header(title)
        print(f"Аккаунтов: {len(accounts)} · параллельно: {self.concurrency()} · пауза: {self.delay()} с\n")
        failures = asyncio.run(
            run_for_accounts(
                accounts, api[0], api[1], self.proxy(),
                self.concurrency(), self.delay(), handler,
            )
        )
        print()
        if failures:
            print(fail(f"успешно {len(accounts) - failures}, ошибок {failures}"))
        else:
            print(ok(f"все {len(accounts)} аккаунтов справились"))
        pause()

    # ---------- 1. аккаунты ----------
    def action_accounts(self) -> None:
        sessions = session_files()
        if not sessions:
            self.show(warn(f"сессий нет. Папка: {SESSION_DIR}"))
            return
        api = self.api()
        if api is None:
            return
        draw_header("Список аккаунтов")
        print("Проверяю сессии...\n")
        self.status = asyncio.run(fetch_status(sessions, api[0], api[1], self.proxy()))
        for index, (name, path) in enumerate(sessions, 1):
            authorized, state = self.status.get(name, (False, "—"))
            marker = paint("✓", "green") if authorized else paint("✗", "red")
            print(f"  {marker} {index:>2}) {paint(f'{name:<16}', 'bold')} {state}")
        print()
        print(paint(f"Папка сессий: {SESSION_DIR}", "dim"))
        pause()

    # ---------- 2. добавление ----------
    def action_add(self) -> None:
        api = self.api()
        if api is None:
            return
        draw_header("Добавление аккаунта")
        phone = ask("Телефон (+79991234567)")
        if not phone:
            return
        default_name = re.sub(r"\D", "", phone) or "account"
        try:
            name = safe_session_name(ask("Имя сессии", default_name))
        except ValueError as exc:
            self.show(fail(str(exc)))
            return
        SESSION_DIR.mkdir(exist_ok=True)
        path = SESSION_DIR / f"{name}.session"

        async def flow() -> None:
            client = make_client(api[0], api[1], path, self.proxy())
            await client.connect()
            try:
                if await client.is_user_authorized():
                    me = await client.get_me()
                    print(ok(f"Сессия «{name}» уже активна: {account_label(me)}"))
                    return
                print("Отправляю код в Telegram...\n")
                sent = await client.send_code_request(phone)
                code = ask("Код из Telegram")
                try:
                    await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
                except errors.SessionPasswordNeededError:
                    await client.sign_in(password=ask_secret("Пароль 2FA"))
                me = await client.get_me()
                self.status[name] = (True, account_label(me))
                print()
                print(ok(f"«{name}» добавлен: {account_label(me)}"))
                print(paint(f"Сессия: {path}", "dim"))
            finally:
                await client.disconnect()

        try:
            asyncio.run(flow())
        except errors.PhoneCodeInvalidError:
            print(fail("неверный код"))
        except errors.PhoneCodeExpiredError:
            print(fail("код истёк, начните заново"))
        except errors.PasswordHashInvalidError:
            print(fail("неверный пароль 2FA"))
        except errors.FloodWaitError as exc:
            print(fail(f"Telegram просит подождать {exc.seconds} с"))
        except Exception as exc:  # noqa: BLE001
            print(fail(human_error(exc)))
        pause()

    # ---------- 3. рассылка ----------
    def action_send(self) -> None:
        if self.api() is None:
            return
        accounts = self.select_accounts(many=True)
        if not accounts:
            return
        draw_header("Отправка сообщения")
        target = ask("Куда (@username, t.me/..., id)")
        if not target:
            return
        try:
            normalize_target(target)
        except ValueError as exc:
            self.show(fail(str(exc)))
            return
        text = ask("Текст сообщения (Enter — взять из файла)")
        if not text:
            file_path = ask("Путь к файлу с текстом")
            if not file_path:
                return
            try:
                text = Path(file_path).expanduser().read_text(encoding="utf-8")
            except OSError as exc:
                self.show(fail(f"не читается файл: {exc}"))
                return
        if not text.strip():
            self.show(fail("пустой текст"))
            return
        print()
        print(f"  Кому:  {paint(target, 'bold')}")
        print(f"  Текст: {text.strip()[:120]}{'…' if len(text.strip()) > 120 else ''}")
        print(f"  От:    {', '.join(name for name, _ in accounts)}")
        if not confirm("\nОтправляем?"):
            return

        async def handler(client, name):
            entity = await resolve_entity(client, target)
            await client.send_message(entity, text, link_preview=True)
            title = getattr(entity, "title", None) or getattr(entity, "username", None) or "чат"
            return f"отправлено в {title}"

        self.run_bulk(accounts, handler, "Отправка сообщения")

    # ---------- 4/5. join / leave ----------
    def action_join(self) -> None:
        if self.api() is None:
            return
        accounts = self.select_accounts(many=True)
        if not accounts:
            return
        draw_header("Вступление в чат/канал")
        target = ask("@username, t.me/..., ссылка-приглашение t.me/+...")
        if not target:
            return
        invite = INVITE_RE.search(target)
        invite_hash = invite.group(1) if invite else None

        async def handler(client, name):
            if invite_hash:
                try:
                    invited = await client(functions.messages.ImportChatInviteRequest(invite_hash))
                except errors.UserAlreadyParticipantError:
                    return "уже участник"
                chats = getattr(invited, "chats", None)
                return f"вступил в {chats[0].title if chats else 'чат'}"
            entity = await resolve_entity(client, target)
            try:
                await client(functions.channels.JoinChannelRequest(entity))
            except errors.UserAlreadyParticipantError:
                return "уже участник"
            return f"вступил в {getattr(entity, 'title', 'чат')}"

        self.run_bulk(accounts, handler, "Вступление в чат/канал")

    def action_leave(self) -> None:
        if self.api() is None:
            return
        accounts = self.select_accounts(many=True)
        if not accounts:
            return
        draw_header("Выход из чата/канала")
        target = ask("@username или ссылка t.me/...")
        if not target:
            return

        async def handler(client, name):
            entity = await resolve_entity(client, target)
            if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
                await client(functions.channels.LeaveChannelRequest(entity))
            else:
                await client(functions.messages.DeleteChatUserRequest(entity.id, "me"))
            return f"вышел из {getattr(entity, 'title', 'чат')}"

        self.run_bulk(accounts, handler, "Выход из чата/канала")

    # ---------- 6. инфо ----------
    def action_info(self) -> None:
        api = self.api()
        if api is None:
            return
        picked = self.select_accounts(many=False)
        if not picked:
            return
        name, path = picked[0]
        draw_header(f"Аккаунт «{name}»")

        async def flow() -> None:
            client = make_client(api[0], api[1], path, self.proxy())
            await client.connect()
            try:
                if not await client.is_user_authorized():
                    print(fail("сессия не авторизована, выполните вход заново"))
                    return
                me = await client.get_me(full=True)
                print(f"  имя:      {' '.join(x for x in [me.first_name, me.last_name] if x) or '—'}")
                print(f"  username: {'@' + me.username if me.username else '—'}")
                print(f"  телефон:  {'+' + str(me.phone) if me.phone else '—'}")
                print(f"  id:       {me.id}")
                print(f"  premium:  {'да' if getattr(me, 'premium', False) else 'нет'}")
                if getattr(me, "full_user", None) is not None:
                    about = (me.full_user.about or "—").strip()
                    print(f"  о себе:   {about[:200]}")
                    print(f"  общих чатов: {getattr(me.full_user, 'common_chats_count', 0)}")
                print(paint(f"  сессия:   {path}", "dim"))
            finally:
                await client.disconnect()

        try:
            asyncio.run(flow())
        except Exception as exc:  # noqa: BLE001
            print(fail(human_error(exc)))
        pause()

    # ---------- 7. удаление ----------
    def action_remove(self) -> None:
        accounts = self.select_accounts(many=True)
        if not accounts:
            return
        draw_header("Удаление аккаунтов")
        for name, _ in accounts:
            print(f"  · {name}")
        print()
        logout = confirm("Завершить сессии и на стороне Telegram (logout)?", True)
        api = self.api() if logout else None
        if logout and api is None:
            return
        if not confirm(f"Удалить выбранные сессии ({len(accounts)})?"):
            return
        removed = 0
        for name, path in accounts:
            if logout and api is not None:
                async def flow(p=path):
                    client = make_client(api[0], api[1], p, self.proxy())
                    await client.connect()
                    try:
                        if await client.is_user_authorized():
                            await client.log_out()
                    finally:
                        try:
                            await client.disconnect()
                        except Exception:  # noqa: BLE001
                            pass

                try:
                    asyncio.run(flow())
                    print(f"  {paint('[logout]', 'dim')} {name}")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {fail(human_error(exc))} {name}")
            for extra in (path, Path(str(path) + "-journal")):
                if extra.exists():
                    extra.unlink()
            self.status.pop(name, None)
            removed += 1
            print(f"  {ok('удалено')} {name}")
        print()
        print(ok(f"готово, удалено сессий: {removed}"))
        pause()

    # ---------- 8. настройки ----------
    def action_settings(self) -> None:
        while True:
            api_id = self.cfg.get("api_id") or os.getenv("TELEGRAM_API_ID")
            api_hash = self.cfg.get("api_hash") or os.getenv("TELEGRAM_API_HASH")
            proxy = self.overrides.get("proxy") or self.cfg.get("proxy") or "—"
            draw_header("Настройки")
            print(f"  1) api_id       {api_id or paint('не задан', 'red')}")
            print(f"  2) api_hash     {('*' * 8 + str(api_hash)[-4:]) if api_hash else paint('не задан', 'red')}")
            print(f"  3) прокси       {proxy}")
            print(f"  4) параллельно  {self.concurrency()}")
            print(f"  5) пауза, с     {self.delay()}")
            print(f"  6) папка сессий {SESSION_DIR}")
            print("  0) назад")
            print()
            choice = ask("Выбор", "0")
            if choice == "0":
                return
            if choice == "1":
                value = ask("api_id")
                if value.isdigit():
                    self.cfg["api_id"] = int(value)
                    save_config(self.cfg)
                    print(ok("сохранено"))
                else:
                    print(fail("api_id — число"))
                pause()
            elif choice == "2":
                value = ask_secret("api_hash")
                if re.fullmatch(r"[0-9a-fA-F]{32}", value or ""):
                    self.cfg["api_hash"] = value
                    save_config(self.cfg)
                    print(ok("сохранено"))
                else:
                    print(fail("api_hash — 32 hex-символа"))
                pause()
            elif choice == "3":
                value = ask("Прокси (socks5://user:pass@host:port), Enter — убрать")
                if not value:
                    self.cfg.pop("proxy", None)
                    save_config(self.cfg)
                    print(ok("прокси убран"))
                else:
                    try:
                        parse_proxy(value)
                    except ValueError as exc:
                        print(fail(str(exc)))
                    else:
                        self.cfg["proxy"] = value
                        save_config(self.cfg)
                        print(ok("сохранено"))
                pause()
            elif choice == "4":
                value = ask("Сколько аккаунтов одновременно", str(self.concurrency()))
                if value.isdigit() and int(value) > 0:
                    self.cfg["concurrency"] = int(value)
                    save_config(self.cfg)
                    print(ok("сохранено"))
                else:
                    print(fail("нужно целое число больше нуля"))
                pause()
            elif choice == "5":
                value = ask("Пауза перед каждым аккаунтом, секунд", str(self.delay()))
                try:
                    delay = float(value.replace(",", "."))
                except ValueError:
                    delay = -1
                if delay >= 0:
                    self.cfg["delay"] = delay
                    save_config(self.cfg)
                    print(ok("сохранено"))
                else:
                    print(fail("нужно неотрицательное число"))
                pause()

    # ---------- главное меню ----------
    def run(self) -> int:
        SESSION_DIR.mkdir(exist_ok=True)
        while True:
            draw_header()
            print("  1) Аккаунты: список и статус")
            print("  2) Добавить аккаунт (вход по номеру)")
            print("  3) Отправить сообщение")
            print("  4) Вступить в чат/канал")
            print("  5) Выйти из чата/канала")
            print("  6) Инфо об аккаунте")
            print("  7) Удалить аккаунт / logout")
            print("  8) Настройки")
            print("  0) Выход")
            print()
            try:
                choice = ask("Выбор", "0")
                if choice in {"0", "q", "exit", "выход"}:
                    print(ok("до встречи"))
                    return 0
                actions = {
                    "1": self.action_accounts,
                    "2": self.action_add,
                    "3": self.action_send,
                    "4": self.action_join,
                    "5": self.action_leave,
                    "6": self.action_info,
                    "7": self.action_remove,
                    "8": self.action_settings,
                }
                action = actions.get(choice)
                if action is None:
                    print(warn("нет такого пункта"))
                    pause()
                else:
                    action()
            except Cancelled:
                print()
                print(ok("выход"))
                return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tg_manager.py",
        description="Интерактивное управление Telegram-аккаунтами. Запустите без аргументов и работайте в меню.",
        epilog=(
            "Пример: ./tg_manager.py\n"
            "Флаги ниже лишь переопределяют config.json для текущего запуска."
        ),
    )
    parser.add_argument("--api-id", help="api_id (иначе env TELEGRAM_API_ID или config.json)")
    parser.add_argument("--api-hash", help="api_hash (иначе env TELEGRAM_API_HASH или config.json)")
    parser.add_argument("--proxy", help="прокси: socks5://user:pass@host:port")
    args = parser.parse_args(argv)

    overrides = {"api_id": args.api_id, "api_hash": args.api_hash, "proxy": args.proxy}
    try:
        return App(overrides).run()
    except Cancelled:
        print()
        return 0


if __name__ == "__main__":
    sys.exit(main())
