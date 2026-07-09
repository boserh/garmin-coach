# Беклог garmin_bot

Напрям розвитку: **від "розумного звіту" до адаптивного AI-тренера** — замкнути петлю
дані → аналіз → план → виконання → порівняння план/факт → автоадаптація.

Аудит 2026-07 (беклог vs код vs ринок, вердикти, RICE нових фіч):
[ANALYSIS.md](ANALYSIS.md).

## Сторі покращення (S/M)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [ST-03](ST-03-weather-in-ondemand-report.md) | Погода в on-demand `/report` і `/report.json` | S | разом з CODE-05 |
| [ST-05](ST-05-strength-preview-in-form.md) | Прев'ю згенерованої силової в setup-формі | M | — (філер, низький пріоритет) |

## Епіки (L/XL)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [EP-03](EP-03-strength-progression.md) | Прогресія силових | L | коли силові стануть регулярними |
| [EP-04](EP-04-web-dashboard.md) | Веб-дашборд | L | EP-01 ✅ (бейджі план/факт) |
| [EP-05](EP-05-race-pack.md) | Race pack — підготовка до перегонів | L | фаза 0: типізувати `target_date`; GAP-модуль спільний з EP-15 |
| [EP-06](EP-06-saas-quotas.md) | SaaS-режим: квоти вартості · ❄️ **frozen**: чужі юзери на неофіційному API, який Garmin закриває — після OPS-01 + 3 міс. стабільності | XL | рішення про продукт |
| [EP-07](EP-07-weekly-digest.md) | Тижневий дайджест і прогрес до цілі | L | ✅ **done** (2026-07) |
| [EP-08](EP-08-health-alerts.md) | Проактивні health-алерти (аномалії відновлення) | L | синергія з NF-01 (пороги) |
| [EP-09](EP-09-ask-full-history.md) | `/ask` над усією історією (tool-use агент над БД) | L–XL | — |
| [EP-10](EP-10-multisport.md) | Мультиспорт: вело/плавання у планах і аналізі | XL | фаза 2 (навантаження) → NF-05 |
| [EP-11](EP-11-web-coach-chat.md) | Веб-чат з тренером | L | EP-09 бажано |
| [EP-12](EP-12-post-run-checkin.md) | Пост-тренувальний check-in (RPE + самопочуття) | M–L | ✅ **MVP done** (2026-07); фази 2–3 (RPE-тренд в адаптацію, статус план/факт) — далі |
| [EP-13](EP-13-weather-aware-week.md) | Погодо-свідоме планування тижня | M–L | ✅ **done** (2026-07) |
| [EP-14](EP-14-personal-records.md) | Особисті рекорди й віхи | M | — |
| [EP-15](EP-15-elevation-gap.md) | Рельєф і grade-adjusted pace (GAP) | M–L | GAP-модуль реюзабельний (його чекає EP-05 фаза 2) |
| [EP-16](EP-16-season-periodization.md) | Сезонна періодизація (кілька стартів) | XL | EP-02 ✅, EP-05; intake спільний з NF-05 |

## Нові фічі (аудит 2026-07; деталі й RICE-розбір — ANALYSIS.md §3)

| ID | Назва | Оцінка | RICE | Залежності |
| --- | --- | --- | --- | --- |
| [NF-01](NF-01-personal-baselines.md) | «Сьогодні vs твоя норма» — довгострокові базлайни | M | 6.3 | — |
| [NF-05](NF-05-multisport-weekly-budget.md) | Мультиспорт-бюджет тижня (кайт/теніс/вело як навантаження) | M | 3.0 | — (= EP-10 фаза 2) |
| [NF-06](NF-06-compare-past-self.md) | «Я-минулорічний» — порівняння з минулим собою | M | 2.0 | — |
| [NF-04](NF-04-injury-risk-radar.md) | Травматичний радар (injury-risk сигнали) | M | 0.9 | EP-08, EP-12 |
| [NF-08](NF-08-personal-mcp-server.md) | Особистий MCP-сервер над БД (експеримент) | M | 0.9 | EP-09 (спільні tool-хелпери) |
| [NF-02](NF-02-correlation-engine.md) | Кореляційний движок — «що на тебе насправді діє» | L | 0.7 | розвідковий прогін перед розробкою (§4.4) |
| [NF-03](NF-03-sickness-travel-mode.md) | Режим «хвороба/подорож» — ремонт плану одним тапом | M | 0.6 | EP-08 бажано |
| [NF-07](NF-07-quarterly-wrapped.md) | Квартальний/річний огляд («Wrapped») | S–M | 0.14 | EP-14 бажано |

## Перфоманс

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [PERF-01](PERF-01-parallel-user-jobs.md) | Паралелізація per-user джоб бота · ❄️ **frozen**: за 1–2 юзерів болю немає, тригер >5 юзерів | M | CODE-04 перед; PERF-03 для >2 |
| [PERF-03](PERF-03-postgres-and-indexes.md) | Postgres перед мультиюзером + індекси · ❄️ **frozen**: прив'язаний до `/register` — той самий garth-ризик, що EP-06 (аудит індексів можна окремо) | M | — |
| [PERF-04b](PERF-04b-async-anthropic-threadpool.md) | AsyncAnthropic + розвантаження threadpool | M | разом з PERF-02/CODE-01 |
| [PERF-05](PERF-05-per-user-fetch-lock-and-garmin-rate-limit.md) | Rate limit/backoff до Garmin + per-user fetch-lock (виживання) | M | об'єднати з OPS-01 (той самий шар клієнта) |

## Безпека й операції (аудит коду 2026-07)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [SEC-01](SEC-01-web-login-hardening.md) | Веб-логін: rate limit, секрет сесії, logout POST | S–M | — · блокер перед відкриттям `/register` |
| [OPS-02](OPS-02-sqlite-backups.md) | Бекапи `garmin.db` (нічний VACUUM INTO + ротація + off-SD копія) | S | — (проби на Pi) |

## Оптимізації коду (рефакторинги)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [CODE-01](CODE-01-split-analysis-service.md) | Розбити `analysis/service.py` (1043 рядки) на пакет | M | до/разом з PERF-02, PERF-04b |
| [CODE-02](CODE-02-cli-push-plan-reuse-plan-sync.md) | CLI `push-plan` поверх `plan_sync` (залишок: відбір вікна) | S | — |
| [CODE-03](CODE-03-remove-legacy-paths.md) | Прибрати legacy: `WEB_TOKEN`, `GARTH_TOKEN_DIR` (`gconn` НЕ видаляти — OPS-01) | S | — |
| [CODE-06](CODE-06-dedup-plan-edit-adapt-stats.md) | Злити `plan_edit_with_stats`/`plan_adapt_with_stats` (AST-ідентичні) | S | разом з CODE-01 |
| [CODE-07](CODE-07-import-fit-series-refactor-tests.md) | Розплутати `import_fit_series` + тести (cyclomatic 20, вкладеність 8, 0 тестів) | S–M | — (низький пріоритет) |

## Рекомендований порядок (2026-07, за ANALYSIS.md §4.1)

**Негайно (виживання):**

1. **OPS-01** ✅ — розвідка python-garminconnect + задокументований план міграції +
   моніторинг падіння логіну зроблені (2026-07); rate limit/backoff — лишився
   в PERF-05 (той самий шар клієнта).
2. **PERF-02** ✅ — Claude-дедуп у таблиці `llm_cache` (спільній для бота й веба),
   Garmin-кеш у per-key файлах; старий `garmin_cache.json` seed'иться один раз
   (2026-07).
3. **PERF-04a** ✅ — bcrypt → `asyncio.to_thread` через async-обгортки в
   `app.core.crypto`; async-роути (login/register/зміна пароля/створення юзера)
   більше не морозять event loop (2026-07).
4. **OPS-02** — бекапи `garmin.db`: скрипт на вечір проти втрати річної історії
   (SD-картка — найтиповіша відмова Pi); ST-08 і SEC-01 — короткі, за нагодою
   (SEC-01 стає блокером лише перед відкриттям `/register`).

**Quick wins (1–2 тижні кожен, високий ефект):** CODE-04 ✅ → EP-07 ✅ (недільний
дайджест; недільний пайплайн ще закласти під злиття з EP-02-пропозиціями і
EP-13-погодою — поки окрема джоба о 19:00) →
**EP-12** ✅ MVP (RPE/болі — годують усе наступне; лишились фази-споживачі) →
**EP-13** ✅ (погодна корекція плану — щоденна легка джоба; липнева спека) →
**CODE-05** ✅ (спільний report-флоу бот/веб/morning) → EP-14 + ST-03 — філери.

**Стратегічні ставки (місяць+ кожна, це і є моат):** **NF-01** (підсилює звіти,
EP-08-пороги і NF-06) → **EP-09** (движок для EP-11 і NF-08) → **NF-05**
(коректність адаптації для реального мультиспорт-профілю) → EP-04 (продуктове
відчуття без LLM-витрат).

**Експерименти (дешеві, перевіряють гіпотези):** NF-02 (один ручний прогін
кореляцій — чи є знахідки взагалі), NF-08 (вихідні + подвійне використання
EP-09-інструментів), NF-07 (раз і в задоволення).

**Заморожено до стабілізації auth:** EP-06 (SaaS), PERF-03 (Postgres), PERF-01
(паралелізація), відкриття `/register`.

Решта — за станом: EP-03 — коли силові стануть регулярними; EP-15 — щойно
з'являться горбисті маршрути; EP-16 — коли буде другий старт у календарі;
EP-10 (аналіз вело) і ST-05 — за запитом/філери.

## Done

| ID | Назва | Де реалізовано |
| --- | --- | --- |
| [ST-01](ST-01-morning-report-plan-context.md) | Ранковий звіт бачить сьогоднішнє тренування з плану | `plan_today` наскрізь у `app/analysis/service.py` (`analyze_with_stats` + cache key) |
| [ST-02](ST-02-extra-metrics-in-reports.md) | `extra`-метрики (readiness, ACWR, RHR) у щоденних звітах | `fitness`-знімок у `run_analysis`/`analyze_with_stats` (`app/analysis/service.py`) |
| [ST-04](ST-04-auto-activity-analysis.md) | Автоаналіз нової пробіжки після синку | `_activity_watch_for_user` у `bot/jobs.py` (вбудований у morning-тік) |
| [ST-06](ST-06-remote-mfa-relogin.md) | Remote MFA re-login | `app/garmin/mfa.py` + `/settings`-флоу (⚠️ спирається на garth-логін — подальша доля в OPS-01) |
| [EP-01](EP-01-plan-vs-actual-matching.md) | План/факт: матчинг виконаних тренувань | `app/garmin/matching.py` + `tests/test_matching.py` |
| [EP-02](EP-02-adaptive-plan.md) | Адаптивний план (замикання петлі) | `plan_adapt_job`/`_adapt_morning_check` у `bot/jobs.py`, `User.plan_adapt_enabled`, `tests/test_plan_adapt*.py` |
| [ST-07](ST-07-plan-adjust-level.md) | Adjust level — межі автоадаптації плану | `intake["adjust_level"]` + `plan_adjust_level`/`_filter_ops_to_level` (`app/analysis/service.py`), правила рівнів у `SYSTEM_PLAN_ADAPT`, вибір на setup-формі + зміна на `/plan` без перегенерації |
| [ST-08](ST-08-validate-exercise-names.md) | Валідувати назви вправ (не лише категорії) перед пушем | `exercises.check_exercise` (`app/garmin/exercises.py`, лог `EXERCISE invalid:`) у `_sanitize_strength` + swap-гілці `apply_plan_ops` (`app/garmin/repository.py`); `exercises_for` живить `exercise_variants` у `run_plan_edit`-контексті + `SYSTEM_PLAN_EDIT`; `tests/test_plan.py` |
| [OPS-01](OPS-01-garmin-auth-plan-b.md) | Garmin auth: «план Б» готовий у шухляді (сама міграція — за фактом поломки garth) | Маркери `GARMIN AUTH FAIL` (`app/garmin/mfa.py`, `providers.py`), `app.cli token-expiry` + `app/garmin/token_info.py`, `scripts/ops01_recon_gconn.py` (recon на Pi: 0 FAIL, garminconnect 0.3.6), план міграції в тікеті. Rate limit — далі в PERF-05 |
| [PERF-02](PERF-02-dedup-cache-to-db.md) | Дедуп-кеші з JSON-файлів у БД (крос-процесний баг) | Таблиця `llm_cache` + `app/db/llm_cache.py` (get/put у `run_analysis`/`run_ask`/`run_activity_analysis`; ключі `_cache_key` недоторкані); Garmin-кеш — per-key файли в `GARMIN_CACHE_DIR` з одноразовим seed'ом зі старого `garmin_cache.json` (`client._seed_legacy_cache`); `tests/test_llm_cache.py` + `tests/test_garmin_disk_cache.py` |
| [PERF-04a](PERF-04a-bcrypt-off-event-loop.md) | bcrypt поза event loop | `hash_password_async`/`verify_password_async` у `app/core/crypto.py` (`asyncio.to_thread`); async-роути `app/routers/auth.py` + `app/routers/settings.py` `await`-ять їх (sync-версії лишились для CLI) |
| [EP-12](EP-12-post-run-checkin.md) | Пост-тренувальний check-in — MVP (RPE + біль) | `ActivityRecord.subjective` (JSON `{rpe, pain?, note?}`) + `repository.set_subjective`/`get_last_activity`; бот `checkin_keyboard`/`checkin_callback` (RPE 1–10 + біль-кнопки, stateless по `ci:` callback data) чіпляється до автоаналізу (`bot/jobs.py`) і `/activity`; ручна `/checkin`; споживач-фаза 1 — `activity_payload` віддає `subjective` у `SYSTEM_ACTIVITY` (і в cache key); `/me` показує RPE/біль; `tests/test_checkin.py`. **Фази 2–3 далі**: RPE-тренд в EP-02-адаптацію, статус план/факт для дайджесту, повтор болю в ранковий звіт |
| [EP-13](EP-13-weather-aware-week.md) | Погодо-свідоме планування тижня | `weather.fetch_forecast_week` + `find_weather_conflicts` (чистий фільтр: ключова сесія на екстрем-день — спека/злива/вітер/ожеледь у межах `WEATHER_DECISION_DAYS`; без конфлікту — нуль Claude-викликів), `SYSTEM_WEATHER_PLAN` + `run_weather_plan_check`/`weather_plan_with_stats` (`app/analysis/service.py`, лише move/modify у вікні, ніколи skip/add; `ReportLog(kind="weather")`), `weather_plan_job`/`_weather_plan_for_user` (`bot/jobs.py`, щоденна `run_daily` о `WEATHER_PLAN_HOUR`, гейт: локація + активний план + `plan_adapt_enabled`), перевикористовує `_send_adapt_proposal`/`adapt_callback`; `_has_pending_proposal` — спільний гейт «одна пропозиція за раз» у всіх трьох хуках (пастка «не смикати двічі»); `tests/test_weather.py` + `tests/test_weather_plan_jobs.py` |
| [EP-07](EP-07-weekly-digest.md) | Тижневий дайджест і прогрес до цілі | `SYSTEM_DIGEST` + `run_digest`/`digest_with_stats`/`_digest_cache_key` (`app/analysis/service.py`, числа рахуємо ми у `_week_volume_summary`), `weekly_digest_job`/`_digest_for_user`/`force_digest_for_user` (`bot/jobs.py`, недільна `run_daily` о `DIGEST_HOUR`, once-a-week guard `bot_state` `digest:<iso-week>`), прихована `/test_digest`, `ReportLog(kind="digest")`; `tests/test_digest.py` |
| [CODE-04](CODE-04-jobs-boilerplate-helpers.md) | Спільні хелпери per-user джоб | `eligible_users` (`app/db/users.py`) + `for_each_user`/`user_garmin_runtime` (`bot/jobs.py`): три джоби (`morning_job`/`plan_sync_job`/`plan_adapt_job`) стали 1–5-рядковими, per-user try/except централізований (одна помилка не рве цикл — тепер і в adapt), guard «є Garmin-креди» спільний для `_tick_for_user`/`_sync_for_user`; логи скіпів/падінь незмінні; `tests/test_jobs.py` |
| [CODE-05](CODE-05-shared-report-delivery.md) | Спільний report-флоу (бот/веб/morning) | `app/analysis/delivery.py::build_report` (payload → run_analysis → text + sync-прапори) + `ReportResult`/`STALE_NOTE`; три виклики зведені: `bot/handlers.py::report`, `app/routers/reports.py::report_json`, `bot/jobs.py::_deliver_morning`. Канал сам формує stale-примітку (Telegram-префікс vs JSON-`note`) й ловить `AnalystError`; morning лишає свій `_MORNING_STALE` («звіт» не «аналіз» — свідома відмінність, бо stale рахує строгіший `_recovery_synced`, не `synced_today`) + guard/вікно/MFA. Дедуп-кеш незмінний (хелпер лише збирає наявний виклик). `tests/test_delivery.py` + `test_routers.py::test_report_json_uses_shared_helper_and_stale_note` |

## Наскрізна пастка

Все, що додається в контекст Claude-виклику, **мусить увійти в dedup-cache key**
(`app/analysis/service.py::_cache_key`), інакше кеш віддаватиме старий звіт без
нового контексту. Стосується всього нового контексту (EP-07-дайджест, NF-01-норми
тощо); ST-03 — ні: `weather` уже в ключі.
