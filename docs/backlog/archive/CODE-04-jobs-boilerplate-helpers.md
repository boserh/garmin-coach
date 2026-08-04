# CODE-04 · Спільні хелпери для per-user джоб (вибірка юзерів + error wrapper)

**Тип:** рефакторинг · **Оцінка:** S · **Пріоритет:** середній (зробити ПЕРЕД
PERF-01 — паралелізація ляже на ці ж хелпери) · **Залежності:** немає

## Проблема

У `bot/jobs.py` тричі скопійований той самий каркас (`morning_job:359-370`,
`plan_sync_job:334-343`, `plan_adapt_job:295-306`):

```python
async with async_session_maker() as session:
    recipients = (await session.execute(
        select(User).where(User.telegram_chat_id.is_not(None),   # ± цей рядок
                           User.is_active.is_(True),
                           User.is_approved.is_(True)))).scalars().all()
    for user in recipients:
        await _x_for_user(ctx, session, user)
```

…плюс у кожному `_x_for_user` повторюється try/except + `logger.exception(
f"... user={user.id}")` + guard'и `user_runtime`/`creds.has_garmin`. Нова джоба
(дайджест EP-07, алерти EP-08) — це четверта й п'ята копія.

## Acceptance criteria

- [ ] `app/db/users.py` (або `bot/jobs.py`): `eligible_users(session, *,
      with_chat: bool)` — одна вибірка active+approved (±chat_id).
- [ ] Один хелпер `for_each_user(worker, *, with_chat, label)` — сесія, вибірка,
      цикл, per-user try/except із стандартним лог-рядком. Три джоби стають
      3–5-рядковими.
- [ ] Guard "є Garmin-креди + runtime" — спільний контекст-хелпер, який
      використовують `_sync_for_user`/`_tick_for_user` замість копій.
- [ ] Поведінка й формат логів незмінні (по логах Pi звіряються скіпи).
- [ ] PERF-01 (паралелізація) потім міняє тільки нутро `for_each_user`.

## Підводні камені

- `morning_job` має додаткові guard'и (вікно годин, once-a-day через `bot_state`)
  **до** циклу і **всередині** per-user — у хелпер тягнути тільки спільний каркас,
  не специфіку.
