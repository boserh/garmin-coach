# PERF-04a · bcrypt поза event loop

**Тип:** перфоманс · **Оцінка:** S (фікс на годину) · **Пріоритет:** високий —
зробити одразу, мимохідь · **Залежності:** немає (виділено з PERF-04 за
ANALYSIS.md §1.3)

## Проблема

**bcrypt в event loop**: `POST /login` (`app/routers/auth.py`) кличе
`verify_password` (bcrypt.checkpw, ~100–300 мс за дизайном) прямо в async-роуті.
Кожен логін підморожує **весь** процес — і `/report.json` інших юзерів, і
morning-тік, якщо бот і веб колись з'їдуться в один процес. Те саме
`hash_password` у створенні юзера/зміні пароля.

## Acceptance criteria

- [x] `verify_password`/`hash_password` у роутах — через `run_in_threadpool`
      (або `asyncio.to_thread`).
- [x] Наявні auth-тести зелені; поведінка логіну не змінилась.

## Реалізовано (2026-07)

`app.core.crypto` отримав async-обгортки `hash_password_async`/`verify_password_async`
(через `asyncio.to_thread`); синхронні версії лишились для sync-викликачів (CLI).
Async-роути `app/routers/auth.py` (login, register) і `app/routers/settings.py`
(зміна пароля, створення юзера) тепер `await`-ять async-варіанти, тож bcrypt більше
не блокує event loop.
