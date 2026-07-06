# PERF-04 · Event loop і threadpool під навантаженням

**Тип:** перфоманс · **Оцінка:** S–M · **Пріоритет:** середній (стає високим разом
із PERF-01) · **Залежності:** немає

## Проблема

Дві споріднені точки:

1. **bcrypt в event loop**: `POST /login` (`app/routers/auth.py:41`) кличе
   `verify_password` (bcrypt.checkpw, ~100–300 мс за дизайном) прямо в async-роуті.
   Кожен логін підморожує **весь** процес — і `/report.json` інших юзерів, і
   morning-тік, якщо бот і веб колись з'їдуться в один процес. Те саме
   `hash_password` у створенні юзера/зміні пароля.

2. **Спільний anyio-threadpool (≈40 потоків) — єдине вузьке горло** для всього
   блокуючого: garth-логіни, кожен `daily_summary` (окремий hop на кожен день),
   activities, planned, **і** всі Claude-виклики (`run_in_threadpool(analyze_…)`,
   які тримають потік секундами). При паралельних юзерах (PERF-01) пул
   вичерпується: швидкі операції стають у чергу за повільними LLM-викликами.

## Acceptance criteria

- [ ] `verify_password`/`hash_password` у роутах — через `run_in_threadpool`
      (або `asyncio.to_thread`).
- [ ] Claude-виклики винесені в **окремий** `ThreadPoolExecutor` (невеликий,
      ~4–8 потоків) або переведені на `AsyncAnthropic` — щоб LLM-латентність не
      з'їдала пул, потрібний Garmin-фетчам. `AsyncAnthropic` — чистіше:
      `analyze_with_stats` і сиблінги стають async, `run_in_threadpool`-обгортки
      зникають (узгодити з PERF-02, який теж чіпає ці функції).
- [ ] Groupped-фетч днів: у `build_payload_cached` пропущені дні тягнути одним
      `run_in_threadpool`-заходом (цикл всередині), а не hop-на-день.
- [ ] Смок-тест: 5 конкурентних `/login` + `/report.json` не деградують у рази.

## Підводні камені

- `AsyncAnthropic` міняє і retry/timeout поведінку — перевірити, що `AnalystError`
  мапиться так само і `ReportLog` пишеться в усіх гілках.
- `_get_client`-пул клієнтів per-key лишити (він і для async-класу потрібен).
