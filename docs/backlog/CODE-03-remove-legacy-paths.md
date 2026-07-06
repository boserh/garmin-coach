# CODE-03 · Прибрати legacy-шляхи: `WEB_TOKEN`, `GARTH_TOKEN_DIR`

**Тип:** рефакторинг/чистка · **Оцінка:** S · **Пріоритет:** середній (філер) ·
**Залежності:** немає (питання `gconn` вирішене — НЕ видаляти, див. нижче)

## Проблема

*(переписано за ANALYSIS.md §1.4 — вердикт по `gconn` перевернувся)*

Дві мертві гілки збільшують площу підтримки й плутають нових читачів:

1. **`WEB_TOKEN` + `app/core/security.py::verify_token`** — legacy shared secret,
   витіснений cookie-сесіями; роути його вже не використовують, але Settings,
   dependency і документація його носять.
2. **`GARTH_TOKEN_DIR`** — глобальний токен-дир доеомної ери; per-user токени
   давно в БД. Використовується хіба `import-garth-token` — той може брати шлях
   аргументом.

**`gconn` зі скоупу вилучено**: після deprecation garth (ANALYSIS.md §0)
`python-garminconnect` — єдина жива бібліотека з Cloudflare-обходом, і «мертвий»
`gconn`-провайдер — кістяк плану Б, а не legacy. Його доля (стати основним
провайдером) — [OPS-01](OPS-01-garmin-auth-plan-b.md).

## Acceptance criteria

- [ ] `verify_token`/`WEB_TOKEN` видалені (Settings, `dependencies.py`, docs);
      якщо десь ще підключені — спершу підтвердити грепом і зняти.
- [ ] `GARTH_TOKEN_DIR` зникає з Settings; `import-garth-token` отримує
      `--path` з дефолтом `~/.garth`.
- [ ] `gconn`-провайдер і `GARMIN_PROVIDER`-світч НЕ чіпати (OPS-01).
- [ ] CLAUDE.md/README оновлені (таблиця env-змінних, розділ providers).
- [ ] Тести, що покривали legacy-гілки, видалені разом із кодом.

## Підводні камені

- Переконатися, що на Pi/у чиємусь `.env` не живе `WEB_TOKEN`-виклик через curl
  у cron — це breaking change для скриптів (юзер один, спитати — досить).
