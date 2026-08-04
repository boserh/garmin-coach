# ST-17 · Приховати активність (дубль / битий трек)

**Тип:** покращення (BA-аналіз 2026-07-23, кат. A — керування даними) · **Оцінка:** M ·
**Пріоритет:** середній · **Поверхня:** обидва · **Залежності:** —

## Проблема

Дубль запису (годинник + телефон), битий GPS (темп 2:10/км) чи випадково синхронізована
чужа активність отруює **все** зверху: `records.detect_records` фіксує фальшивий рекорд,
`multisport.weekly_load` завищує бюджет, `matching` закриває планову сесію не тим бігом,
`compare`/`wrapped`/`goal` рахують сміття у статистику. Видалити рядок можна лише SQL-ем
— і навіть тоді наступний фетч Garmin поверне його назад (`upsert_activity` вставить
знову). EP-14 частково захищається (пейс-флор 2:30/км), але це лата, не контроль.

## User story

Як користувач я хочу приховати конкретну активність, щоб вона зникла з усіх
агрегатів, рекордів і матчингу — і не поверталась після наступного синку.

## Обсяг

**Входить:** колонка `ActivityRecord.is_hidden` (default False, міграція); кнопка
«🙈 Приховати» / «Показати» на `/me`-детейлі активності (з confirm); `/hide <id>` у
боті; виключення прихованих з усіх читачів repository (list_activities, window_stats,
weekly_run_volume, weekly_activity_load, read_load_history, recent_subjective_runs,
query_activities …) і з `records.detect_records`; видалення `PersonalRecord`-рядків,
чий `activity_id` вказує на щойно приховану активність; стійкість до upsert
(`upsert_activity` не скидає прапорець).

**Не входить:** фізичне видалення рядка (навмисно — ресинк повертав би його);
автоматичне виявлення дублів; перерахунок週 тижневих/VO2max-рекордів, не привʼязаних
до `activity_id` (окремий `backfill-records` уже вміє пересіяти).

## Acceptance criteria

- [ ] Прихована активність не зʼявляється у `/activities`, `/me`, дашборді, `/ask`-
      інструментах (`query_activities`/`get_activity_detail` → «не знайдено»),
      window_stats/weekly_* агрегатах і в контекстах LLM-викликів.
- [ ] `records.detect_records` ігнорує приховані; рекорди з `activity_id` прихованої
      видаляються при ховʼанні (з підказкою запустити `backfill-records` для
      пересіву решти категорій).
- [ ] Наступний `build_payload_cached` НЕ повертає активність у видиме (upsert
      оновлює поля, але не `is_hidden`).
- [ ] Якщо активність була зматчена з сесією плану — матч знімається
      (`completed_activity_id=None`, статус повертається у `planned`/`missed`).
- [ ] Тести: ізоляція по юзеру, upsert-стійкість, зникнення з агрегатів.

## Технічні дотики

- `app/db/models.py` + Alembic-міграція (`is_hidden`).
- `app/garmin/repository/core.py`, `stats.py`: `.where(ActivityRecord.is_hidden
  .is_(False))` у читачах; `app/records.py`; `app/garmin/matching.py` (unmatch).
- `app/routers/me.py`: `POST /me/activities/{id}/hide`; `bot/handlers.py::hide`.

## Вартість LLM / запити до Garmin

0 / 0. Побічно **зменшує** витрати: сміттєва активність більше не тягне автоаналіз
у контекст і не ламає dedup-ключі агрегатів.

## Ризики

- Пропустити одного читача — активність «просочиться» (мітігація: єдиний helper
  `visible_activities()`-фільтр у repository, грепнути всі `select(ActivityRecord)`).

## RICE

Reach 2/міс · Impact 2 · Confidence 0.9 · Effort ~3 дні → **Score ≈ 1.2**
