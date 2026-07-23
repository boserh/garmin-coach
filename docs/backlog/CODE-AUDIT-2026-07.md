# Аудит рефакторингу — липень 2026

> Перевірено проти коду на гілці станом на 2026-07-23: розміри файлів, grep
> по викликах, vulture/ruff-скан, читання гарячих шляхів (`bot/jobs.py::_tick_for_user`,
> `app/analysis/reports.py`, `app/cli.py`). Усі CODE-01…07 закриті — це наступна хвиля.
> Пункти незалежні один від одного; жоден не міняє поведінку.

## TL;DR

| # | Що | Тип | Оцінка | Ефект |
| --- | --- | --- | --- | --- |
| A1 | Спільний рушій `run_*`-нарацій у `reports.py` (7 майже ідентичних копій) | дублювання | S–M | −250…300 рядків, новий вид звіту = ~15 рядків |
| A2 | Один генеричний cache-key білдер замість 5 у `cache.py` | дублювання | S | −60 рядків |
| A3 | Декоратор для бот-команд (`session → _resolve_user → AnalystError`) | дублювання | M | −150…200 рядків у `handlers.py` |
| A4 | CLI-преамбула (`init_db → get_by_email → user_runtime → login`) як context manager | дублювання | S | −80…100 рядків у `cli.py` |
| A5 | `_mean`/`_avg`/`_median` ×5 + розкидані форматери дат/темпу → один модуль | дублювання | S | дрібне, але прибирає 5-те визначення `_mean` |
| B1 | `repository.py` (1621 рядок, ~60 функцій) → пакет за доменами | громіздкість | M | найбільший файл проєкту |
| B2 | `bot/jobs.py` (1118) → morning-тік / тижневі джоби / sync окремо | громіздкість | M | — |
| B3 | `tests/test_routers.py` (1421) → по файлу на роутер | громіздкість | S | — |
| C1 | Мертвий код: `analyze()`, sync `build_payload()`, `delete_schedule`, `SONNET_4_6` | чистка | S | — |
| C2 | Разові fix-команди CLI (`fix-stride-paces`, `convert-easy-hr`) — відправити на пенсію | чистка | S | −280 рядків |
| D1 | Детектори ризику рахуються до 3× за один тік — рахувати раз | оптимізація | S | −⅔ зайвих DB-читань у гарячому шляху |
| D2 | `_weather_chips` на кожен GET `/plan` ходить в Open-Meteo — короткий TTL-кеш | оптимізація | S | — |

---

## A. Дублювання

### A1 · Спільний рушій кешованих нарацій (сиквел CODE-06)

CODE-06 злив AST-ідентичні `plan_edit_with_stats`/`plan_adapt_with_stats` — але на
нараційному боці той самий блок досі скопійований ~7 разів у
`app/analysis/reports.py`: `run_compare` (:940), `run_wrapped` (:996),
`run_race_plan` (:1048), `run_insights` (:1125), `run_digest` (:835),
`run_activity_analysis` (:763) і (з варіаціями) `run_analysis`. Скелет однаковий:

```
key = _X_cache_key(context, MODEL_X)
cached = await llm_cache.get(session, key)
if cached: text, stats = cached, CallStats(cached=True)
else:
    try: text, stats = await _run_claude(x_with_stats, context, api_key)
    except AnalystError: await repository.log_report(..., ok=False); raise
    await llm_cache.put(session, key, text, CACHE_TTL_S)
await repository.log_report(..., ok=True, ...)
return text
```

Витягти `_run_cached_narration(session, *, user_id, kind, model, context,
with_stats_fn, cache_key, question, max_...)`, а кожен `run_*` лишити тонкою
обгорткою «зібрати контекст → перевірити has_signal → викликати рушій».
Сигнатури `*_with_stats` НЕ чіпати — тести їх monkeypatch'ать (урок CODE-06).
Різницю run_digest/run_analysis (додаткові поля question/report_text) покрити
параметрами, не форком.

### A2 · Генеричний cache-key білдер

`_digest_cache_key`/`_insights_cache_key`/`_wrapped_cache_key`/`_race_cache_key`/
`_compare_cache_key` (`app/analysis/cache.py:132–215`) — одна й та сама форма:
вибрати поля з context + model + маркер виду → `sha256(json)`. Один
`_context_cache_key(kind: str, context: dict, model: str, fields: tuple)`
замінює всі п'ять; докстрінги про «README pitfall» переїжджають до нього.
`_cache_key`/`_ask_cache_key`/`_activity_cache_key` мають свою логіку — не чіпати.

### A3 · Декоратор бот-команд

~20 хендлерів у `bot/handlers.py` повторюють каркас: `async with
async_session_maker()` → `_resolve_user` → early-return → (опц. `load_credentials`)
→ `try/except AnalystError → reply_text(str(e))` (12 копій try/except). Декоратор
на кшталт `@bot_command(creds=True)` що інжектить `(session, user, creds)` зрізав
би 150–200 рядків і зробив би обробку AnalystError гарантовано однаковою
(зараз тексти/логи трохи розходяться між командами). Це та сама ідея, що CODE-04
зробив для джоб (`for_each_user`/`user_garmin_runtime`) — тепер для команд.

### A4 · CLI-преамбула

~10 команд у `app/cli.py` (усі `_backfill_*`, `_push_plan`, `_unpush_plan`,
`_fix_stride_paces`, `_convert_easy_hr`, …) повторюють: `await init_db()` →
сесія → `users.get_by_email` → «User not found» → `user_runtime(session, user)` →
`run_in_threadpool(get_provider().login)`. Один async context manager
`cli_user(email, *, garmin=False)` прибирає по ~10 рядків з кожної команди й
одне місце, де майбутня OPS-01-міграція логіну правиться один раз.

### A5 · Розкидані мікро-хелпери

- `_mean` визначений тричі: `app/injury.py:122`, `app/correlations.py:51`,
  `app/subjective.py:59`; поруч `_avg`/`_median` у `app/garmin/repository.py:607`.
  Один `app/statutil.py` (avg/median/mean) — і чотири модулі імпортують його.
- Форматери дат/темпу: `plan.py::_dow/_dm`, `bot/jobs.py::_dow_label`,
  `records.py::_fmt_pace`, `me.py::_pace_str` — кандидати в спільний
  `app/format.py` (за бажанням; цінність нижча, ніж у `_mean`).

Свідомо НЕ чіпати: `fueling.estimate_minutes` vs `plan.py::_est_minutes` — дубль
задокументований як навмисний (core-модуль не повинен залежати від роутера).

---

## B. Громіздкі файли

### B1 · `app/garmin/repository.py` — 1621 рядок, ~60 функцій

Найбільший файл проєкту; фактично 6 доменів в одному неймспейсі: daily/activities,
records, plans+workouts, reports/costs, bot_state+pending-edits, віконна
статистика (`window_stats`/`wrapped_stats`/`weekly_*`). Розбити на пакет
`app/garmin/repository/` (`daily.py`, `activities.py`, `plans.py`, `reports.py`,
`state.py`, `stats.py`) з фасадом `__init__`, що реекспортує все — рівно за
рецептом CODE-01 (зовнішні імпорти `from app.garmin import repository` і
monkeypatch-шляхи в тестах лишаються робочими, нуль поведінкових змін).

### B2 · `bot/jobs.py` — 1118 рядків

Після CODE-04 каркас спільний, але файл змішує три незалежні шари: morning-тік
з 8 хуками (`_tick_for_user` + `_token_expiry/_records/_injury/_health/_deload/
_adapt_morning/_extend_nudge`), тижневі/місячні джоби (digest, compare, insights,
adapt) і daily-sync (plan_sync, race pack, gear). Пакет `bot/jobs/`
(`morning.py`, `weekly.py`, `sync.py`, `shared.py` для `for_each_user`/`user_tz`/
`_send_adapt_proposal`) з фасадним `__init__` — той самий безпечний прийом.

### B3 · `tests/test_routers.py` — 1421 рядок

Один файл на всі роутери. Розбити на `test_routers_auth/plan/me/…` — чисто
механічно, пришвидшує локальний прогін одного шматка.

`app/analysis/prompts.py` (957) — не проблема: це текст промптів, розмір чесний.
`bot/handlers.py` (1171) і `reports.py` (1277) зникають зі списку самі собою
після A1/A3.

---

## C. Мертвий код

Перевірено grep'ом по `app`/`bot`/`tests` + vulture:

- **`app/analysis/reports.py:225 analyze()`** — нуль викликів (лишився тільки
  реекспорт у фасаді `service.py:123`). Прибрати разом з реекспортом.
- **`app/garmin/service.py:311 build_payload()`** (синхронний) — викликається
  лише з `tests/test_garmin_service.py`; докстрінг каже «CLI / fallback», але CLI
  його не використовує. Або чесно перепідписати «test harness only», або
  прибрати й перевести тести на `_fetch_days`.
- **`app/garmin/client.py:513 delete_schedule()`** — нуль викликів (unpush іде
  через `delete_workout`; видалення воркаута зносить і розклад). Прибрати або
  задокументувати як API-симетрію.
- **`app/analysis/client.py:34 SONNET_4_6`** — константа ніде не використовується
  (ціна в `PRICES` живе окремим рядком-ключем). Однорядкова чистка + реекспорт.
- **Legacy-міграція `garmin_cache.json` → `.migrated`** у `client.py` —
  одноразова; після підтвердження, що на Pi файл уже `.migrated`, гілку можна
  прибрати (низький пріоритет).
- **НЕ мертве** (не чіпати): `gconn`/`providers.py` — це кістяк плану Б OPS-01
  (вердикт ANALYSIS.md §0); синк-обгортки `*_with_stats` — їх monkeypatch'ать
  тести; ORM-колонки/роути, які vulture позначає «unused» — false positive.

### C2 · Разові fix-команди CLI

`_fix_stride_paces` (~80 рядків + `_parse_pace_ranges`/`_stride_pace_from_desc`/
`_strides_to_pace`) і `_convert_easy_hr` (~100 рядків + `_convert_easy_steps`) у
`app/cli.py` — одноразові data-fix утиліти під конкретний історичний стан БД.
Якщо вони своє відпрацювали — видалити (git-історія їх збереже); якщо ні —
винести в `scripts/`, щоб `cli.py` лишився тільки живими командами. Разом з A4
це стискає `cli.py` з 942 приблизно до ~500 рядків.

---

## D. Оптимізації

### D1 · Детектори ризику рахуються до 3× за тік

У `_tick_for_user` (кожні 20 хв, вікно 07–23): `_deload_check_for_user` викликає
`build_injury_assessment` **і** `build_health_alerts`; коли deload не вистрілив,
`_injury_check_for_user` знову рахує `build_injury_assessment`, а
`_health_check_for_user` — знову `build_health_alerts`. Кожен виклик — свій набір
читань 90-денної історії (`read_load_history`/`recent_subjective_runs`/
`read_history`/`count_daily_metrics`). Виправлення: порахувати обидва assessment'и
один раз у `_tick_for_user` і передати вниз параметрами — guard-логіка не
міняється, зникає ⅔ зайвих читань у найгарячішому шляху бота. Бонусом можна
гейтити самі детектори на morning-вікно (зараз вони крутяться весь день, хоча DM
і так заблоковані guard'ами).

### D2 · `_weather_chips` — живий фетч на кожен рендер `/plan`

Задокументоване v1-рішення (Open-Meteo безкоштовний), але кожен GET сторінки —
мережевий запит у `run_in_threadpool`. TTL-кеш на ~15 хв (у пам'яті процесу,
ключ — координати) прибирає латентність повторних відкриттів сторінки. Дрібниця,
робити за нагоди.

### Не-проблеми (перевірено, щоб не здавалося)

- `run_analysis` читає 90-денну історію один раз і ділить її між `norm` і
  `health.detect` — уже оптимізовано (ST-10).
- Індекси гарячих читань на місці (PERF-03 slice); дедуп-кеш у БД (PERF-02);
  Claude-пул окремо від anyio (PERF-04b).

---

## Порядок, якщо братися

1. **A1 + A2** (одним заходом — обидва в `analysis/`, найбільший зріз рядків).
2. **C1 + C2** (чистка, нульовий ризик, робиться за годину).
3. **D1** (маленький, але в найгарячішому шляху).
4. **A3, A4** (механічні, великі за площею — окремими PR).
5. **B1, B2, B3** (розбиття файлів фасадним прийомом CODE-01 — тільки після A*,
   щоб не переносити дублікати в нові файли).
