# CODE-03 · Прибрати legacy-шляхи: `WEB_TOKEN`, `gconn`, `GARTH_TOKEN_DIR`

**Тип:** рефакторинг/чистка · **Оцінка:** S · **Пріоритет:** середній ·
**Залежності:** рішення користувача по `gconn` (видалити vs протестувати)

## Проблема

Три мертві/напівмертві гілки збільшують площу підтримки й плутають нових читачів:

1. **`WEB_TOKEN` + `app/core/security.py::verify_token`** — legacy shared secret,
   витіснений cookie-сесіями; роути його вже не використовують, але Settings,
   dependency і документація його носять.
2. **`gconn`-провайдер** (`app/garmin/providers.py`) — «untested against the live
   API — do not rely on it» прямо в CLAUDE.md, висить у TODO з заснування.
   Кожна зміна клієнтського шару вимагає думати про другу реалізацію, яку ніхто
   не запускав.
3. **`GARTH_TOKEN_DIR`** — глобальний токен-дир доеомної ери; per-user токени
   давно в БД. Використовується хіба `import-garth-token` — той може брати шлях
   аргументом.

## Acceptance criteria

- [ ] `verify_token`/`WEB_TOKEN` видалені (Settings, `dependencies.py`, docs);
      якщо десь ще підключені — спершу підтвердити грепом і зняти.
- [ ] `gconn`: **або** видалити провайдер + `GARMIN_PROVIDER`-світч (легко
      повернути з git), **або** один раз прогнати проти живого API і зняти
      "untested" — рішення зафіксувати тут перед виконанням.
- [ ] `GARTH_TOKEN_DIR` зникає з Settings; `import-garth-token` отримує
      `--path` з дефолтом `~/.garth`.
- [ ] CLAUDE.md/README оновлені (таблиця env-змінних, розділ providers).
- [ ] Тести, що покривали legacy-гілки, видалені разом із кодом.

## Підводні камені

- Переконатися, що на Pi/у чиємусь `.env` не живе `WEB_TOKEN`-виклик через curl
  у cron — це breaking change для скриптів (юзер один, спитати — досить).
