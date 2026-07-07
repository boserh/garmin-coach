# PERF-02 · Дедуп-кеші з JSON-файлів у БД

**Тип:** перфоманс/коректність · **Оцінка:** M · **Пріоритет:** високий (баг уже при
двох процесах) · **Залежності:** немає · **Статус:** ✅ зроблено (2026-07)

## Проблема

`claude_cache.json` (`app/analysis/service.py:90-115`) і `garmin_cache.json`
(`app/garmin/client.py`, ті самі патерни) — це module-level dict, завантажений
**один раз при імпорті**, який на кожен запис серіалізує і перезаписує **весь файл**
(`os.replace`):

1. **Два процеси** (бот + uvicorn) тримають незалежні копії: web-звіт не бачить
   кеш-хітів бота і навпаки → подвійна оплата тих самих Claude-викликів;
   last-writer-wins при збереженні **мовчки губить** записи іншого процесу.
2. Файл росте лінійно з юзерами (кожен юзер — свої ключі), а піковий запис — це
   повний rewrite: при сотнях записів кожен Claude-виклик тягне за собою
   серіалізацію всього кешу.
3. Немає жодного локу навіть у межах процесу (запис із threadpool-потоків).

## Acceptance criteria

- [x] Нова таблиця `llm_cache` (`key` PK, `value` TEXT, `expires_at`) + міграція;
      читання/запис по одному ключу, TTL-очистка лінивим DELETE.
- [x] `garmin_cache.json` (immutable assets: exercise/workout/series) — так само або
      залишити файл, але з per-key файлами/append-only, якщо таблиця здасться зайвою.
- [x] Обидва процеси бачать спільний кеш; `CLAUDE CACHE HIT` працює крос-процесно.
- [x] Ключі та TTL-семантика не змінюються (головна пастка з README беклогу —
      логіка `_cache_key` недоторкана).
- [x] Старі JSON-файли підхоплюються один раз як seed (або просто ігноруються —
      кеш прогріється сам за тиждень TTL).

## Як реалізовано (2026-07)

- **Claude-кеш → БД**: таблиця `llm_cache` (`key` sha256-hex PK, `value` TEXT,
  `expires_at` epoch float + індекс; міграція `b7e4a9c1d2f3`). Модуль
  `app/db/llm_cache.py` — async `get`/`put`; `put` upsert'ить через `merge` і
  лінивим DELETE вичищає прострочені рядки. Обидва хелпери best-effort: падіння
  кешу ніколи не ламає аналіз (failed read = miss, failed write = warning +
  rollback, щоб сесія лишалась придатною для ReportLog).
- **Перевірка піднята в async-обгортки** (як і планувалось): `run_analysis`,
  `run_ask`, `run_activity_analysis` рахують ключ тими самими незмінними
  `_cache_key`/`_ask_cache_key`/`_activity_cache_key`, роблять `llm_cache.get`
  (хіт → `CLAUDE CACHE HIT` у лог + `ReportLog(cached=True)`, як раніше) і `put`
  після успішного виклику. Синхронні `*_with_stats` стали чистими API-викликами
  без кешу (вони бігають у threadpool без доступу до async-БД); back-compat
  `analyze()` тепер без кешу (живих викликачів нема).
- **Garmin-кеш → per-key файли** (варіант з AC — таблиця тут зайва, бо `client.py`
  синхронний і без сесії): один JSON-файл на ключ у `GARMIN_CACHE_DIR`
  (`garmin_cache/`, `series:v1:<id>` → `series_v1_<id>.json`), атомарний запис
  через tmp+`os.replace`, поверх — in-process memo. Крос-процесність: міс/протухле
  в memo → перечитування файлу (підхоплює запис іншого процесу).
- **Seed**: старий `garmin_cache.json` **розщеплюється** в per-key файли один раз
  при імпорті (`_seed_legacy_cache`) і перейменовується в `.migrated` — його
  series/exercise-записи живуть рік, і перевикачування сотень series з Garmin
  ризикує 429. Старий `claude_cache.json` **ігнорується** (дозволено AC): його
  ключі включають сьогоднішню дату, тож seed дав би максимум день користі.
- Конфіг: + `GARMIN_CACHE_DIR`; `CLAUDE_CACHE_FILE` прибрано (`GARMIN_CACHE_FILE`
  лишився як джерело seed'у).
- Тести: `tests/test_llm_cache.py` (roundtrip/TTL/purge, **два engine до одного
  SQLite-файлу** — крос-процесний хіт, hit-шлях `run_analysis`/`run_ask` з
  підрахунком викликів, `cached=True` у ReportLog), `tests/test_garmin_disk_cache.py`
  (per-key файли, читання з файлу при холодному memo, seed з legacy-файлу без
  перезапису свіжішого).

## План по файлах

- `app/db/models.py` + `alembic/` — модель `LlmCache`, міграція.
- `app/analysis/service.py` — `_load_cache/_save_cache/_cache` → асинхронні
  get/put по ключу (виклики вже мають `session` під рукою в `run_*` обгортках).
- `app/garmin/client.py` — те саме для disk-кешу (або окремим PR).
- `tests/` — кеш-хіт через "другий процес" (два engine до одного SQLite-файлу).

## Підводні камені

- `analyze_with_stats` та сиблінги — **синхронні** (викликаються через
  `run_in_threadpool`); доступ до async-БД зсередини них неможливий. Кеш-перевірку
  доведеться підняти рівнем вище, в `run_analysis`/`run_ask`/… — це і є правильне
  місце (там уже є session).
- Не забути, що `garmin_cache.json` серіалізує великі series (~150 точок × сотні
  активностей) — у БД це просто TEXT, ок.
- (виявлене в роботі) Повторний ідентичний `/ask` протягом 5 хв — **не** хіт, і це
  правильно: перша відповідь потрапляє в `recent_asks`-тред, який є частиною ключа
  (так було і зі старим файловим кешем).
