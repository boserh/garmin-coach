# ST-02 · `extra`-метрики (readiness, ACWR, RHR…) у щоденних звітах

**Тип:** покращення · **Оцінка:** S · **Пріоритет:** високий · **Залежності:** немає
(синергія зі ST-01: readiness + сьогоднішня сесія = повноцінна порада)

## Сторя

> Як користувач, я хочу, щоб щоденний звіт враховував Training Readiness, ACWR
> (навантаження), recovery time, RHR-тренд, VO2max і race predictions — щоб порада
> «бігти/відпочити» спиралась на всі зібрані дані, а не лише на HRV+сон.

## Контекст

У `DailyMetric.extra` вже лежить усе: readiness score/level/feedback, ACWR %/feedback,
acute load, recovery time, RHR, SpO2, respiration, VO2max, race predictions, endurance.
CLAUDE.md прямо фіксує: «used by the reports: not yet» — цей знімок зараз їсть лише
генерація плану (`run_plan_generation`). Жодного нового фетчу з Garmin не потрібно.

## Acceptance criteria

- [ ] `run_analysis` (kind `report`/`morning`) передає в контекст компактний
      `fitness`-знімок — той самий coalesce з `repository.get_recent_extra`
      (найсвіжіше non-null значення по кожному ключу за ~21 день).
- [ ] `SYSTEM`-промпт пояснює поля і правила реакції: високий ACWR / довгий recovery
      time / дрейф RHR вгору → обережність; не переказувати всі числа у звіті, а
      використовувати для висновку.
- [ ] Порожній знімок (новий користувач без історії) → звіт працює як зараз.
- [ ] `fitness` включено в dedup-cache key; повторний `/report` того ж дня без нових
      даних лишається кеш-хітом.
- [ ] Розмір доданого контексту контрольований (плоский dict скалярів, без масивів).

## План по файлах

- `app/analysis/service.py` —
  - `run_analysis` (~:516): для не-deep викликати `repository.get_recent_extra(session,
    user_id)` і передати як `fitness` (той самий формуючий код, що в
    `run_plan_generation` — за можливості винести спільний билдер знімка);
  - `analyze_with_stats` (~:172): параметр `fitness: Optional[dict]` → `user_content`;
  - `_cache_key` (~:121): включити `fitness` у хеш.
- `app/analysis/prompts.py` — блок у `SYSTEM` з поясненням полів (`acwr_pct`,
  `recovery_time_h`, `readiness_score`, `resting_hr`…) і правилами інтерпретації —
  можна переиспользовать формулювання з `SYSTEM_PLAN`, де ці ж поля вже описані.
- `tests/` — тест, що знімок потрапляє в контент і в cache key; тест поведінки без даних.

## Підводні камені

- **Cache key**: `get_recent_extra` повертає «найсвіжіше по ключу» — знімок змінюється
  щодня з новими даними, це ок (новий день = новий звіт і так). Але переконатися, що
  порядок ключів у dict не робить хеш нестабільним (сортувати при хешуванні —
  подивитись, як `_cache_key` вже серіалізує payload).
- Не дублювати те, що вже є в `daily[]` (HRV/сон/стрес там уже є) — брати лише
  extra-поля, інакше контекст роздувається без користі.
