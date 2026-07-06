# PERF-03 · Postgres перед мультиюзером + індекси user-scoped запитів

**Тип:** перфоманс/операційний enabler · **Оцінка:** M · **Пріоритет:** високий перед
відкриттям `/register` для чужих людей · **Залежності:** розблоковує повноцінний PERF-01

## Проблема

SQLite (`aiosqlite`) — один writer на всю базу. Уже зараз конкурентно пишуть:
морнінг-тік (upsert daily/activities + ReportLog), веб-запити (`/report.json`,
`/plan`), plan-sync джоба. При кількох юзерах і паралелізації джоб (PERF-01)
почнуться `database is locked` / `SQLITE_BUSY`.

Перемикання вже закладене архітектурно — `DATABASE_URL=postgresql+asyncpg://...`
без змін коду. Бракує: перевіреного проходу Alembic-ланцюжка на Postgres,
docker-compose/systemd-нотаток і аудиту індексів.

## Acceptance criteria

- [ ] `alembic upgrade head` проходить на чистому Postgres 16 (в т.ч. JSON-колонки,
      `server_default`, batch-операції, які на SQLite поводяться інакше).
- [ ] Повний тест-сьют зелений на Postgres (CI-джоба або локальний compose).
- [ ] Аудит індексів під user-scoped читання (усі гарячі запити фільтрують по
      `user_id` + дата/час): композитні індекси
      `daily_metrics(user_id, date)`, `activities(user_id, start_time)`,
      `report_logs(user_id, created_at)`, `planned_workouts(plan_id, date)` —
      додати ті, яких бракує, однією міграцією.
- [ ] Скрипт/нотатка одноразової міграції даних SQLite → Postgres
      (де-факто: `import-export` + `import-fit-series` уже вміють відбудувати
      історію з GDPR-експорту — можливо, цього досить, зафіксувати рішення).
- [ ] README/CLAUDE.md — розділ «деплой із Postgres».

## Підводні камені

- SQLite лишається дефолтом для одноюзерного Pi — нічого не ламати, тільки
  додати перевірений другий шлях.
- `JSON`-колонки: у SQLAlchemy на Postgres це `JSON` (не JSONB) без явного typу —
  для наших читань "цілим значенням" це ок, індексація всередину не потрібна.
- Пул `asyncpg`: виставити `pool_size` свідомо (бот + веб = два пули до однієї БД).
