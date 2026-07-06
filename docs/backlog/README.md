# Беклог garmin_bot

Напрям розвитку: **від "розумного звіту" до адаптивного AI-тренера** — замкнути петлю
дані → аналіз → план → виконання → порівняння план/факт → автоадаптація.

## Сторі покращення (S/M)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [ST-01](ST-01-morning-report-plan-context.md) | ~~Ранковий звіт бачить сьогоднішнє тренування з плану~~ ✅ | S | — |
| [ST-02](ST-02-extra-metrics-in-reports.md) | ~~`extra`-метрики (readiness, ACWR, RHR) у щоденних звітах~~ ✅ | S | — |
| [ST-03](ST-03-weather-in-ondemand-report.md) | Погода в on-demand `/report` і `/report.json` | S | — |
| [ST-04](ST-04-auto-activity-analysis.md) | ~~Автоаналіз нової пробіжки після синку~~ ✅ | M | — |
| [ST-05](ST-05-strength-preview-in-form.md) | Прев'ю згенерованої силової в setup-формі | M | — |
| [ST-06](ST-06-remote-mfa-relogin.md) | ~~Remote MFA re-login~~ ✅ | M | — (enabler для деплою на Pi) |

## Епіки (L/XL)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [EP-01](EP-01-plan-vs-actual-matching.md) | ~~План/факт: матчинг виконаних тренувань~~ ✅ | L | — |
| [EP-02](EP-02-adaptive-plan.md) | ~~Адаптивний план (замикання петлі)~~ ✅ | L | EP-01 |
| [EP-03](EP-03-strength-progression.md) | Прогресія силових | L | — |
| [EP-04](EP-04-web-dashboard.md) | Веб-дашборд | L | EP-01 (бейджі план/факт — опційно) |
| [EP-05](EP-05-race-pack.md) | Race pack — підготовка до перегонів | L | — |
| [EP-06](EP-06-saas-quotas.md) | SaaS-режим: квоти вартості | XL | рішення про продукт |
| [EP-07](EP-07-weekly-digest.md) | Тижневий дайджест і прогрес до цілі | L | EP-01 ✅ |
| [EP-08](EP-08-health-alerts.md) | Проактивні health-алерти (аномалії відновлення) | L | — |
| [EP-09](EP-09-ask-full-history.md) | `/ask` над усією історією (tool-use агент над БД) | L–XL | — |
| [EP-10](EP-10-multisport.md) | Мультиспорт: вело/плавання у планах і аналізі | XL | — |
| [EP-11](EP-11-web-coach-chat.md) | Веб-чат з тренером | L | EP-09 бажано |
| [EP-12](EP-12-post-run-checkin.md) | Пост-тренувальний check-in (RPE + самопочуття) | M–L | ST-04 ✅ |
| [EP-13](EP-13-weather-aware-week.md) | Погодо-свідоме планування тижня | M–L | — |
| [EP-14](EP-14-personal-records.md) | Особисті рекорди й віхи | M | — |
| [EP-15](EP-15-elevation-gap.md) | Рельєф і grade-adjusted pace (GAP) | M–L | — |
| [EP-16](EP-16-season-periodization.md) | Сезонна періодизація (кілька стартів) | XL | EP-02 ✅, EP-05 |

## Перфоманс при зростанні кількості юзерів

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [PERF-01](PERF-01-parallel-user-jobs.md) | Паралелізація per-user джоб бота | M | CODE-04 перед; PERF-03 для ліміту >2 |
| [PERF-02](PERF-02-dedup-cache-to-db.md) | Дедуп-кеші з JSON-файлів у БД (крос-процесний баг) | M | — |
| [PERF-03](PERF-03-postgres-and-indexes.md) | Postgres перед мультиюзером + індекси | M | — |
| [PERF-04](PERF-04-event-loop-and-threadpool.md) | Event loop і threadpool під навантаженням | S–M | — |
| [PERF-05](PERF-05-per-user-fetch-lock-and-garmin-rate-limit.md) | Per-user fetch-lock і rate limit до Garmin | M | разом з PERF-01 |

## Оптимізації коду (рефакторинги)

| ID | Назва | Оцінка | Залежності |
| --- | --- | --- | --- |
| [CODE-01](CODE-01-split-analysis-service.md) | Розбити `analysis/service.py` (1043 рядки) на пакет | M | до/разом з PERF-02, PERF-04 |
| [CODE-02](CODE-02-cli-push-plan-reuse-plan-sync.md) | CLI `push-plan` поверх `plan_sync` (дубль вікна) | S | — |
| [CODE-03](CODE-03-remove-legacy-paths.md) | Прибрати legacy: `WEB_TOKEN`, `gconn`, `GARTH_TOKEN_DIR` | S | рішення по `gconn` |
| [CODE-04](CODE-04-jobs-boilerplate-helpers.md) | Спільні хелпери per-user джоб | S | перед PERF-01 |
| [CODE-05](CODE-05-shared-report-delivery.md) | Спільний report-флоу (бот/веб/morning) | S–M | разом зі ST-03 |

## Рекомендований порядок

ST-01 → ST-02 → ST-04 → **EP-01 → EP-02** (ядро цінності) → ST-06 (перед деплоєм
на Raspberry Pi) → EP-04 → EP-05. ST-03 і ST-05 — філери між епіками. EP-03 —
коли силові стануть регулярними. EP-06 — тільки якщо буде рішення продавати.

Нова черга (2026-07): **PERF-02** (крос-процесний кеш-баг — уже сьогодні, не «при
рості») → **EP-07** (дешевий, сильний ефект) → CODE-04 → PERF-01 + PERF-05 →
PERF-03 (перед відкриттям `/register` чужим) → EP-08 → EP-09 → EP-11.
CODE-01 — разом з першим PR, що серйозно чіпає `analysis/service.py`;
CODE-02/03/05 — філери. PERF-04 — разом з PERF-01 або EP-11 (streaming).
EP-10 — за запитом (не-біговий сезон / triathlon-ціль).

Друга хвиля фіч (EP-12–16): **EP-12** (check-in — дешевий, живить EP-02/07/08) і
**EP-14** (рекорди — чистий Python, fun) — гарні філери хоч зараз; **EP-15** (GAP) —
щойно з'являться горбисті маршрути; **EP-13** (погода тижня) — сезонно, перед
літньою спекою; **EP-16** (сезон) — коли буде другий старт у календарі.

## Наскрізна пастка

Все, що додається в контекст Claude-виклику, **мусить увійти в dedup-cache key**
(`app/analysis/service.py::_cache_key`), інакше кеш віддаватиме старий звіт без
нового контексту. Стосується ST-01, ST-02 (ST-03 — ні: `weather` уже в ключі).
