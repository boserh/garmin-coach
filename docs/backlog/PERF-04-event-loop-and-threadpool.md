# PERF-04 · Event loop і threadpool під навантаженням — розбито (2026-07)

Тікет змішував S- і M-задачу (ANALYSIS.md §1.3) і розбитий на два:

- [PERF-04a](PERF-04a-bcrypt-off-event-loop.md) — bcrypt поза event loop
  (S, фікс на годину — зробити одразу);
- [PERF-04b](PERF-04b-async-anthropic-threadpool.md) — AsyncAnthropic +
  розвантаження threadpool (M, разом з PERF-02/CODE-01).

Історичний текст — у git
(`git log -- docs/backlog/PERF-04-event-loop-and-threadpool.md`).
