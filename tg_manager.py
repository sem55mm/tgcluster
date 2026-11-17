#!/usr/bin/env python3
"""
Массовое управление Telegram-аккаунтами через Telethon.

Все файлы сессий лежат в папке sessions/ рядом с этим скриптом.
api_id / api_hash берутся из переменных окружения
(TELEGRAM_API_ID, TELEGRAM_API_HASH) или из config.json рядом со скриптом
(создаётся командой `setup`).

Примеры:
  ./tg_manager.py setup
  ./tg_manager.py add --phone +79991234567 --name work
  ./tg_manager.py list
  ./tg_manager.py send @durov "привет" --all
  ./tg_manager.py send https://t.me/somechat "текст" -a work,spare --delay 2
  ./tg_manager.py join https://t.me/+AbCdEfGh "текст" -a work
  ./tg_manager.py leave @somechat --all
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
    sys.stderr.write(
        "Не установлен Telethon. Выполните:\n"
        "  pip install -r requirements.txt\n"
    )
    raise SystemExit(2)

BASE_DIR = Path(__file__).resolve().parent
SESSION_DIR = BASE_DIR / "sessions"
CONFIG_PATH = BASE_DIR / "config.json"

INVITE_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)"
)
PUBLIC_RE = re.compile(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:s/)?([A-Za-z0-9_]{3,})")


# --------------------------------------------------------------------------- #
# Конфигурация и утилиты
# --------------------------------------------------------------------------- #
def die(msg: str, code: int = 1):
    sys.stderr.write(f"Ошибка: {msg}\n")
    raise SystemExit(code)


def load_credentials(args) -> tuple[int, str]:
    api_id = getattr(args, "api_id", None) or os.getenv("TELEGRAM_API_ID")
    api_hash = getattr(args, "api_hash", None) or os.getenv("TELEGRAM_API_HASH")

    if (not api_id or not api_hash) and CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            die(f"не читается {CONFIG_PATH.name}: {exc}")
        api_id = api_id or cfg.get("api_id")
        api_hash = api_hash or cfg.get("api_hash")

    if not api_id or not api_hash:
        die(
            "не заданы api_id/api_hash. Получите их на https://my.telegram.org "
            "и выполните `./tg_manager.py setup` (или задайте TELEGRAM_API_ID "
            "и TELEGRAM_API_HASH в окружении)."
        )
    try:
        api_id = int(api_id)
    except (TypeError, ValueError):
        die("api_id должен быть числом")
    return api_id, str(api_hash)


def parse_proxy(value: str | None):
    """socks5://user:pass@host:port | socks5://host:port | host:port -> dict для Telethon."""
    if not value:
        return None
    raw = value.strip()
    scheme = "socks5"
    if "://" in raw:
        scheme, raw = raw.split("://", 1)
    scheme = scheme.lower()
    if scheme not in {"socks5", "socks4", "http"}:
        die(f"неизвестная схема прокси: {scheme} (socks5, socks4, http)")
    auth = None
    if "@" in raw:
        auth, raw = raw.rsplit("@", 1)
    if ":" not in raw:
        die("прокси указывается как host:port или scheme://user:pass@host:port")
    host, port = raw.rsplit(":", 1)
    try:
        port = int(port)
    except ValueError:
        die(f"некорректный порт прокси: {port}")
    proxy = {"proxy_type": scheme, "addr": host, "port": port, "rdns": True}
    if auth:
        user, _, password = auth.partition(":")
        proxy["username"] = user
        proxy["password"] = password
    return proxy


def safe_session_name(name: str) -> str:
    if not name or not name.strip():
        die("пустое имя сессии")
    cleaned = re.sub(r"[^A-Za-z0-9_.+-]", "_", name.strip())
    if cleaned in {".", ".."}:
        die("недопустимое имя сессии")
    return cleaned


def session_files() -> list[tuple[str, Path]]:
    """[(имя_аккаунта, путь_к_файлу_сессии)] из папки sessions/."""
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


def make_client(api_id: int, api_hash: str, path: Path, proxy) -> "TelegramClient":
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


def select_accounts(accounts: str | None, use_all: bool) -> list[tuple[str, Path]]:
    sessions = session_files()
    if not sessions:
        die(f"в {SESSION_DIR} нет сессий. Сначала добавьте аккаунт: ./tg_manager.py add")
    if use_all:
        return sessions
    if not accounts:
        available = ", ".join(name for name, _ in sessions)
        die(f"укажите --all или -a имя1,имя2. Доступные аккаунты: {available}")
    mapping = dict(sessions)
    chosen: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for item in accounts.split(","):
        name = item.strip()
        if not name or name in seen:
            continue
        if name not in mapping:
            die(f"нет сессии «{name}». Доступные: {', '.join(mapping)}")
        chosen.append((name, mapping[name]))
        seen.add(name)
    if not chosen:
        die("не выбрано ни одного аккаунта")
    return chosen


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [max([len(headers[i])] + [len(str(r[i])) for r in rows]) for i in range(len(headers))]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def human_error(exc: Exception) -> str:
    if isinstance(exc, errors.FloodWaitError):
        return f"FloodWait: нужно подождать {exc.seconds} с"
    if isinstance(exc, errors.SessionPasswordNeededError):
        return "нужен пароль двухфакторной аутентификации (2FA)"
    if isinstance(exc, errors.AuthKeyUnregisteredError):
        return "сессия отозвана, нужен повторный вход (add)"
    if isinstance(exc, errors.UserDeactivatedBanError):
        return "аккаунт заблокирован"
    if isinstance(exc, errors.ChatWriteForbiddenError):
        return "нет права писать в этот чат"
    if isinstance(exc, errors.ChatAdminRequiredError):
        return "нужны права администратора"
    if isinstance(exc, errors.ChannelPrivateError):
        return "канал/чат приватный или аккаунт не участник"
    if isinstance(exc, errors.UserAlreadyParticipantError):
        return "аккаунт уже участник"
    if isinstance(exc, errors.InviteHashExpiredError):
        return "ссылка-приглашение истекла"
    if isinstance(exc, errors.InviteHashInvalidError):
        return "ссылка-приглашение недействительна"
    return f"{type(exc).__name__}: {exc}"


async def run_for_accounts(
    accounts: list[tuple[str, Path]],
    api_id: int,
    api_hash: str,
    proxy,
    concurrency: int,
    delay: float,
    handler,
) -> int:
    """Прогоняет handler(client, name) для каждого аккаунта. Возвращает число ошибок."""
    semaphore = asyncio.Semaphore(max(1, concurrency))
    failures = 0
    lock = asyncio.Lock()
    done = 0
    total = len(accounts)

    async def worker(name: str, path: Path) -> None:
        nonlocal failures, done
        async with semaphore:
            if delay:
                await asyncio.sleep(delay)
            client = None
            try:
                client = make_client(api_id, api_hash, path, proxy)
                await client.connect()
                if not await client.is_user_authorized():
                    raise RuntimeError("сессия не авторизована, выполните add")
                result = await handler(client, name)
                message = str(result) if result else "ок"
                async with lock:
                    print(f"[ok]   {name}: {message}", flush=True)
            except Exception as exc:  # noqa: BLE001 — отчёт по каждому аккаунту
                failures += 1
                async with lock:
                    print(f"[fail] {name}: {human_error(exc)}", flush=True)
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:  # noqa: BLE001
                        pass
                async with lock:
                    done += 1
                    if total > 1:
                        print(f"       ({done}/{total})", flush=True)

    await asyncio.gather(*(worker(name, path) for name, path in accounts))
    return failures


# --------------------------------------------------------------------------- #
# Команды
# --------------------------------------------------------------------------- #
def cmd_setup(args) -> int:
    api_id = args.api_id or input("api_id: ").strip()
    api_hash = args.api_hash or input("api_hash: ").strip()
    try:
        api_id_int = int(api_id)
    except ValueError:
        die("api_id должен быть числом")
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash or ""):
        die("api_hash должен состоять из 32 hex-символов")
    CONFIG_PATH.write_text(
        json.dumps({"api_id": api_id_int, "api_hash": api_hash}, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    print(f"Сохранено в {CONFIG_PATH}")
    return 0


def cmd_add(args) -> int:
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)
    phone = (args.phone or input("Телефон (+79991234567): ")).strip()
    if not phone:
        die("телефон не указан")
    name = safe_session_name(args.name or re.sub(r"\D", "", phone))
    SESSION_DIR.mkdir(exist_ok=True)
    path = SESSION_DIR / f"{name}.session"

    async def flow() -> int:
        client = make_client(api_id, api_hash, path, proxy)
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                print(f"Сессия «{name}» уже активна: {account_label(me)}")
                return 0
            sent = await client.send_code_request(phone)
            code = input("Код из Telegram: ").strip()
            try:
                await client.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
            except errors.SessionPasswordNeededError:
                password = getpass.getpass("Пароль 2FA: ")
                await client.sign_in(password=password)
            me = await client.get_me()
            print(f"Аккаунт «{name}» добавлен: {account_label(me)}")
            print(f"Сессия: {path}")
            return 0
        finally:
            await client.disconnect()

    try:
        return asyncio.run(flow())
    except errors.PhoneCodeInvalidError:
        die("неверный код подтверждения")
    except errors.PhoneCodeExpiredError:
        die("код подтверждения истёк, попробуйте снова")
    except errors.PasswordHashInvalidError:
        die("неверный пароль 2FA")
    except errors.FloodWaitError as exc:
        die(f"Telegram просит подождать {exc.seconds} с перед повторной попыткой")
    return 1


def gather_account_info(name: str, path: Path, api_id: int, api_hash: str, proxy):
    async def flow():
        client = make_client(api_id, api_hash, path, proxy)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                return name, path, None, "не авторизована"
            me = await client.get_me()
            return name, path, me, None
        finally:
            await client.disconnect()

    try:
        return asyncio.run(flow())
    except Exception as exc:  # noqa: BLE001
        return name, path, None, human_error(exc)


def cmd_list(args) -> int:
    sessions = session_files()
    if not sessions:
        print(f"Сессий нет. Папка: {SESSION_DIR}")
        return 0
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)

    rows = []
    for name, path in sessions:
        _, _, me, error = gather_account_info(name, path, api_id, api_hash, proxy)
        if me is None:
            rows.append([name, "-", "-", "-", error or "не авторизована"])
            continue
        size_kb = f"{path.stat().st_size / 1024:.0f} КБ"
        rows.append([
            name,
            str(me.id),
            "@" + me.username if me.username else "-",
            "+" + str(me.phone) if me.phone else "-",
            (" ".join(x for x in [me.first_name, me.last_name] if x) or "-") + f" ({size_kb})",
        ])
    print_table(["аккаунт", "id", "username", "телефон", "имя"], rows)
    print(f"\nВсего сессий: {len(sessions)}  (папка: {SESSION_DIR})")
    return 0


def cmd_info(args) -> int:
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)
    name = safe_session_name(args.name)
    path = SESSION_DIR / f"{name}.session"
    if not path.exists():
        die(f"нет сессии «{name}». Доступные: {', '.join(n for n, _ in session_files()) or '—'}")

    async def flow():
        client = make_client(api_id, api_hash, path, proxy)
        await client.connect()
        try:
            if not await client.is_user_authorized():
                die("сессия не авторизована, выполните add")
            me = await client.get_me(full=True)
            print(f"аккаунт:   {name}")
            print(f"id:        {me.id}")
            print(f"имя:       {' '.join(x for x in [me.first_name, me.last_name] if x) or '—'}")
            print(f"username:  {'@' + me.username if me.username else '—'}")
            print(f"телефон:   {'+' + str(me.phone) if me.phone else '—'}")
            print(f"premium:   {'да' if getattr(me, 'premium', False) else 'нет'}")
            print(f"сессия:    {path}")
            if getattr(me, "full_user", None) is not None:
                full = me.full_user
                print(f"о себе:    {(full.about or '—')[:200]}")
                print(f"участник:  {getattr(full, 'common_chats_count', 0)} общих чатов")
            return 0
        finally:
            await client.disconnect()

    return asyncio.run(flow())


def cmd_remove(args) -> int:
    name = safe_session_name(args.name)
    path = SESSION_DIR / f"{name}.session"
    if not path.exists():
        die(f"нет сессии «{name}»")
    if not args.yes:
        answer = input(f"Удалить сессию «{name}»? [y/N]: ").strip().lower()
        if answer not in {"y", "yes", "д", "да"}:
            print("Отменено")
            return 0
    if args.logout:
        api_id, api_hash = load_credentials(args)
        proxy = parse_proxy(args.proxy)

        async def flow():
            client = make_client(api_id, api_hash, path, proxy)
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
            print("Сессия завершена на стороне Telegram")
        except Exception as exc:  # noqa: BLE001
            print(f"Не удалось выполнить logout: {human_error(exc)}", file=sys.stderr)
    for extra in (path, Path(str(path) + "-journal")):
        if extra.exists():
            extra.unlink()
    print(f"Удалено: {path}")
    return 0


def normalize_target(target: str):
    raw = target.strip()
    if INVITE_RE.search(raw) or "+" in raw or "joinchat/" in raw:
        return None  # приглашение нельзя превратить в entity, нужно join
    raw = re.sub(r"^https?://", "", raw)
    raw = re.sub(r"^(t\.me|telegram\.me|telegram\.dog)/", "", raw)
    raw = raw.split("?")[0].strip("/").lstrip("@")
    if not raw:
        die("пустая цель")
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


async def resolve_entity(client, target: str):
    parsed = normalize_target(target)
    if parsed is None:
        raise RuntimeError("это ссылка-приглашение: сначала вступите командой join")
    return await client.get_entity(parsed)


def cmd_send(args) -> int:
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)
    accounts = select_accounts(args.accounts, args.all)

    text = args.text
    if args.file:
        text = Path(args.file).read_text(encoding="utf-8")
    if not text or not text.strip():
        die("пустой текст сообщения (укажите текст или --file)")
    if normalize_target(args.target) is None and not args.file:
        pass  # приглашение обработается в resolve_entity

    async def handler(client, name):
        entity = await resolve_entity(client, args.target)
        await client.send_message(
            entity,
            text,
            silent=args.silent,
            link_preview=not args.no_preview,
        )
        title = getattr(entity, "title", None) or getattr(entity, "username", None) or "чат"
        return f"отправлено в {title} ({len(text)} симв.)"

    failures = asyncio.run(
        run_for_accounts(
            accounts, api_id, api_hash, proxy, args.concurrency, args.delay, handler
        )
    )
    print(f"\nИтог: успешно {len(accounts) - failures}, ошибок {failures}")
    return 1 if failures else 0


def cmd_join(args) -> int:
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)
    accounts = select_accounts(args.accounts, args.all)
    invite_match = INVITE_RE.search(args.target)
    invite_hash = invite_match.group(1) if invite_match else None

    async def handler(client, name):
        if invite_hash:
            try:
                invited = await client(functions.messages.ImportChatInviteRequest(invite_hash))
            except errors.UserAlreadyParticipantError:
                return "уже участник"
            chat = getattr(invited, "chats", None)
            title = chat[0].title if chat else "чат"
            return f"вступил в {title}"
        entity = await resolve_entity(client, args.target)
        try:
            await client(functions.channels.JoinChannelRequest(entity))
        except (errors.UserAlreadyParticipantError, errors.ChannelInvalidError):
            return "уже участник"
        return f"вступил в {getattr(entity, 'title', 'чат')}"

    failures = asyncio.run(
        run_for_accounts(
            accounts, api_id, api_hash, proxy, args.concurrency, args.delay, handler
        )
    )
    print(f"\nИтог: успешно {len(accounts) - failures}, ошибок {failures}")
    return 1 if failures else 0


def cmd_leave(args) -> int:
    api_id, api_hash = load_credentials(args)
    proxy = parse_proxy(args.proxy)
    accounts = select_accounts(args.accounts, args.all)

    async def handler(client, name):
        entity = await resolve_entity(client, args.target)
        if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
            await client(functions.channels.LeaveChannelRequest(entity))
        else:
            await client(functions.messages.DeleteChatUserRequest(entity.id, "me"))
        return f"вышел из {getattr(entity, 'title', 'чат')}"

    failures = asyncio.run(
        run_for_accounts(
            accounts, api_id, api_hash, proxy, args.concurrency, args.delay, handler
        )
    )
    print(f"\nИтог: успешно {len(accounts) - failures}, ошибок {failures}")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def add_mass_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-a", "--accounts", help="имена сессий через запятую: work,spare")
    parser.add_argument("--all", action="store_true", help="все сессии из папки sessions/")
    parser.add_argument("-c", "--concurrency", type=int, default=3, help="сколько аккаунтов одновременно (по умолчанию 3)")
    parser.add_argument("--delay", type=float, default=0.0, help="пауза в секундах перед каждым аккаунтом")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg_manager.py",
        description="Массовое управление Telegram-аккаунтами. Сессии — в папке sessions/ рядом со скриптом.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Примеры:\n"
            "  %(prog)s --api-id 12345 --api-hash <hash> add --phone +79991234567 --name work\n"
            "  %(prog)s list\n"
            "  %(prog)s send @durov 'текст' --all --delay 2\n"
            "  %(prog)s join https://t.me/+AbCdEfGh -a work,spare\n"
            "  %(prog)s remove work --logout\n"
        ),
    )
    parser.add_argument("--api-id", help="api_id (иначе env TELEGRAM_API_ID или config.json)")
    parser.add_argument("--api-hash", help="api_hash (иначе env TELEGRAM_API_HASH или config.json)")
    parser.add_argument("--proxy", help="прокси для всех аккаунтов: socks5://user:pass@host:port")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("setup", help="сохранить api_id/api_hash в config.json")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("add", help="войти в аккаунт и сохранить сессию")
    p.add_argument("--phone", help="номер телефона в международном формате")
    p.add_argument("-n", "--name", help="имя сессии (по умолчанию — цифры телефона)")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="список аккаунтов и их состояние")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("info", help="подробности по одному аккаунту")
    p.add_argument("name", help="имя сессии")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("send", help="отправить сообщение от выбранных аккаунтов")
    p.add_argument("target", help="@username, ссылка t.me/... или id чата")
    p.add_argument("text", nargs="?", help="текст сообщения")
    p.add_argument("--file", help="взять текст из файла (UTF-8)")
    p.add_argument("--silent", action="store_true", help="без звука у получателя")
    p.add_argument("--no-preview", action="store_true", help="не разворачивать превью ссылок")
    add_mass_args(p)
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("join", help="вступить в чат/канал")
    p.add_argument("target", help="@username, ссылка t.me/... или t.me/+invite")
    add_mass_args(p)
    p.set_defaults(func=cmd_join)

    p = sub.add_parser("leave", help="выйти из чата/канала")
    p.add_argument("target", help="@username или ссылка t.me/...")
    add_mass_args(p)
    p.set_defaults(func=cmd_leave)

    p = sub.add_parser("remove", help="удалить файл сессии (опционально с logout)")
    p.add_argument("name", help="имя сессии")
    p.add_argument("--logout", action="store_true", help="завершить сессию на стороне Telegram")
    p.add_argument("-y", "--yes", action="store_true", help="без подтверждения")
    p.set_defaults(func=cmd_remove)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    SESSION_DIR.mkdir(exist_ok=True)
    return args.func(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write("\nПрервано\n")
        sys.exit(130)
