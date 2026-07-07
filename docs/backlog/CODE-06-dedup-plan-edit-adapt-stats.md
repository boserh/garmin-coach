# CODE-06 · Злити `plan_edit_with_stats` і `plan_adapt_with_stats` (AST-ідентичні)

**Тип:** рефакторинг · **Оцінка:** S · **Пріоритет:** низький (робити разом з
CODE-01, який і так розносить цей файл) · **Залежності:** CODE-01

## Проблема

`plan_edit_with_stats` (`app/analysis/service.py:840`) і `plan_adapt_with_stats`
(`app/analysis/service.py:1010`) — **структурно ідентичні** (AST-ізоморфні,
similarity 0.90 за redundancy-аналізом): обидві збирають user-повідомлення,
кличуть `_complete`, парсять відповідь у `PlanEdit` з одним ретраєм. Відрізняються
лише системним промптом (`SYSTEM_PLAN_EDIT` vs `SYSTEM_PLAN_ADAPT`) і складом
контексту. Кожен фікс у парсингу/ретраї зараз треба робити двічі — класичний
дрейф-ризик.

Дрібніший близнюк там само: `repository.get_plan` / `repository.get_activity`
(`app/garmin/repository.py:405` / `:79`) — той самий user-scoped `select ... where
id == ... and user_id == ...`. Це можна лишити як є (2 × 5 рядків), але якщо
з'явиться третій — узагальнити.

## Acceptance criteria

- [ ] Одна спільна функція (напр. `_plan_ops_with_stats(system_prompt, context,
      *, model, ...)`), яку кличуть обидва публічні обгортки — сигнатури
      `plan_edit_with_stats`/`plan_adapt_with_stats` **не змінюються** (їх
      monkeypatch'ать тести і кличуть `run_plan_edit`/`run_plan_adaptation`).
- [ ] Поведінкових змін нуль: `ReportLog.kind` («plan_edit»/«plan_adapt»),
      формат логів `claude`, ретрай-логіка — як були; тест-сьют зелений.
- [ ] Якщо робиться разом з CODE-01 — спільна функція одразу їде в
      `app/analysis/plans.py`, а не додається у монолітний `service.py`.

## Підводні камені

- Адаптація свідомо **не** дедуп-кешується (`_complete` без кешу) — спільний
  хелпер не повинен «за компанію» додати кеш обом.
- Патч-таргети в тестах (`tests/test_plan_adapt*.py`, `test_plan.py`) можуть
  цілитись у приватні атрибути — перевірити грепом перед перейменуваннями.
