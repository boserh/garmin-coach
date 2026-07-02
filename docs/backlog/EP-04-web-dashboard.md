# EP-04 · Веб-дашборд

**Тип:** епік · **Оцінка:** L · **Пріоритет:** середній ·
**Залежності:** самодостатній; бейджі план/факт підтягнуться після EP-01

## Сторя

> Як користувач, я хочу одну сторінку-дашборд: готовність сьогодні, тренди
> відновлення за 30 днів, найближчі тренування плану (з виконанням), останні
> пробіжки і вартість AI за місяць — замість ходіння по сирих таблицях `/me`.

## Контекст

З TODO: «dashboard/history visualization». Зараз веб = форми + сирі таблиці зі
спарклайнами на detail-сторінках. Усі дані вже в БД, всі запити вже написані
(`read_history`, `get_recent_extra`, `list_workouts`, `list_activities`,
агрегати по `report_logs`); SVG-чарти вже є в `app/routers/admin.py`
(`_run_series`/`_run_charts`) — їх треба лише винести і переиспользовать.
Жодного виклику Garmin чи Claude — дашборд суто читає БД (швидкий і безкоштовний).

## Acceptance criteria

- [ ] `GET /dashboard` (логін): блок «сьогодні» (readiness, HRV-статус, сон, body
      battery — з останнього `DailyMetric`+`extra`); тренди 30 дн (HRV, RHR, сон,
      стрес — SVG як існуючі спарклайни, з hover); наступні 7 днів плану
      (тип/дистанція/опис, після EP-01 — бейджі статусів); останні 5 активностей
      (лінк на `/me`-деталь); вартість AI поточного місяця (сума `report_logs`).
- [ ] Логін не-адміна веде на `/dashboard` (замість `/settings`); адмін — як зараз.
- [ ] Сторінка — чистий DB-read: жодного Garmin/Claude виклику, рендер < ~100 мс.
- [ ] Mobile-first (основний сценарій — телефон із Telegram-а); стиль існуючих
      темплейтів.
- [ ] PWA-мінімум: `manifest.json` + іконка, «Add to Home Screen» дає повноекранний
      застосунок.
- [ ] Порожні стани: нема плану / нема історії → акуратні заглушки з лінком на
      `/plan` і `/settings`.

## План по файлах

- **Новий** `app/routers/dashboard.py` — `GET /dashboard`; збір даних:
  `repository.read_history(30)`, `get_recent_extra`, `get_active_plan` +
  `list_workouts` (вікно 7 дн), `list_activities(5)`, новий агрегат
  `repository.month_cost(user_id)` (SUM cost_usd за поточний місяць — його ж
  потім переиспользует EP-06).
- **Новий** `app/charts.py` — винести SVG-хелпери з `app/routers/admin.py`
  (`_run_series`, `_run_charts` і функції побудови спарклайнів `/me`); admin
  імпортує звідси (поведінка без змін).
- **Новий** `app/templates/dashboard.html` — extends наявного base; hover-патерн
  узяти з `detail.html` (інлайн vanilla JS, прогресивне покращення).
- `app/main.py` — зареєструвати router; roothandler `/` для залогінених →
  redirect `/dashboard`.
- `app/routers/auth.py` — post-login redirect не-адмінів: `/settings` → `/dashboard`.
- `app/garmin/repository.py` — `month_cost(session, user_id)`; за потреби
  розширити `read_history` полями з `extra` (RHR уже там — звірити).
- `static/manifest.json` + іконка; лінк у base-темплейті.
- `tests/` — route: логін-гейт, рендер із даними і з порожньою БД; `month_cost`.

## Підводні камені

- Не тягнути chart-бібліотеку — наявні SVG-спарклайни тримають сторінку легкою
  і без збірки фронтенду; якщо їх забракне, розширювати `app/charts.py`, а не
  вводити JS-стек.
- Рефакторинг admin-хелперів — окремим комітом до фічі (щоб diff дашборда був
  чистим і admin-регресія була видима одразу).
- `read_history` капить дні — для 30 дн ок; не піддаватись спокусі «рік історії
  на головній» (повільно на Pi + нечитабельно на телефоні).
