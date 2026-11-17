# tgcluster

CLI для массового управления Telegram-аккаунтами (Telethon). Все файлы сессий
хранятся в папке `sessions/` рядом со скриптом `.py`.

## Установка

```bash
pip install -r requirements.txt
```

Нужны `api_id` и `api_hash` с https://my.telegram.org → API development tools.
Сохраните их один раз:

```bash
./tg_manager.py setup
```

Либо задайте переменные окружения `TELEGRAM_API_ID` и `TELEGRAM_API_HASH`.

## Добавление аккаунтов

```bash
./tg_manager.py add --phone +79991234567 --name work
./tg_manager.py add --phone +79997654321 --name spare
```

Скрипт спросит код из Telegram (и пароль 2FA, если он включён) и создаст
`sessions/work.session`, `sessions/spare.session`.

## Команды

| Команда | Что делает |
| --- | --- |
| `setup` | сохранить api_id/api_hash в `config.json` |
| `add` | войти в аккаунт, сохранить сессию |
| `list` | таблица аккаунтов: id, username, телефон, имя, состояние |
| `info NAME` | подробности по одному аккаунту |
| `send TARGET TEXT` | отправить сообщение от выбранных аккаунтов |
| `join TARGET` | вступить в чат/канал (в т.ч. по ссылке-приглашению) |
| `leave TARGET` | выйти из чата/канала |
| `remove NAME [--logout]` | удалить сессию (с `--logout` — завершить и на сервере) |

Выбор аккаунтов: `--all` (все сессии) или `-a work,spare`.
Дополнительно: `-c/--concurrency` (сколько аккаунтов одновременно, по умолчанию 3),
`--delay` (пауза в секундах перед каждым аккаунтом).

## Примеры

```bash
./tg_manager.py list

./tg_manager.py send @durov "привет" --all --delay 2
./tg_manager.py send https://t.me/somechat "текст" -a work,spare
./tg_manager.py send @somechat --file message.txt --all --silent

./tg_manager.py join https://t.me/+AbCdEfGhIj -a work
./tg_manager.py leave @somechat --all

./tg_manager.py remove spare --logout
```

Глобальные флаги (`--api-id`, `--api-hash`, `--proxy`) ставятся **до** подкоманды:

```bash
./tg_manager.py --proxy socks5://user:pass@127.0.0.1:1080 send @durov "текст" --all
```

## Замечания

- `sessions/` и `config.json` добавлены в `.gitignore`: файл сессии — это полный
  доступ к аккаунту, его нельзя передавать и коммитить.
- Аккаунты работают параллельно; ошибки по каждому аккаунту печатаются отдельно,
  код возврата `1`, если хотя бы один аккаунт не справился (в том числе `FloodWait`).
- Отправка сообщений и вступление в чаты должны укладываться в
  [правила Telegram](https://core.telegram.org/api/terms): массовые рассылки
  незнакомым людям ведут к блокировке аккаунтов.
