# Refactoring audit — July 2026

> Verified against the code on the branch as of 2026-07-23: file sizes, call-site
> greps, a vulture/ruff scan, and a read-through of the hot paths
> (`bot/jobs.py::_tick_for_user`, `app/analysis/reports.py`, `app/cli.py`).
> All of CODE-01…07 are closed — this is the next wave. The items are independent
> of each other; none of them changes behaviour.

## TL;DR

| # | What | Kind | Size | Payoff |
| --- | --- | --- | --- | --- |
| A1 | Shared engine for the `run_*` narrations in `reports.py` (7 near-identical copies) | duplication | S–M | −250…300 lines; a new report kind = ~15 lines |
| A2 | One generic cache-key builder instead of 5 in `cache.py` | duplication | S | −60 lines |
| A3 | Decorator for bot commands (`session → _resolve_user → AnalystError`) | duplication | M | −150…200 lines in `handlers.py` |
| A4 | CLI preamble (`init_db → get_by_email → user_runtime → login`) as a context manager | duplication | S | −80…100 lines in `cli.py` |
| A5 | `_mean`/`_avg`/`_median` ×5 + scattered date/pace formatters → one module | duplication | S | small, but kills the 5th definition of `_mean` |
| B1 | `repository.py` (1621 lines, ~60 functions) → a package split by domain | bulk | M | the project's largest file |
| B2 | `bot/jobs.py` (1118) → morning tick / weekly jobs / sync as separate modules | bulk | M | — |
| B3 | `tests/test_routers.py` (1421) → one file per router | bulk | S | — |
| C1 | Dead code: `analyze()`, sync `build_payload()`, `delete_schedule`, `SONNET_4_6` | cleanup | S | — |
| C2 | One-off CLI fix commands (`fix-stride-paces`, `convert-easy-hr`) — retire | cleanup | S | −280 lines |
| D1 | Risk detectors computed up to 3× per tick — compute once | optimization | S | −⅔ of redundant DB reads in the hot path |
| D2 | `_weather_chips` hits Open-Meteo on every GET `/plan` — short TTL cache | optimization | S | — |

---

## A. Duplication

### A1 · Shared engine for cached narrations (CODE-06's sequel)

CODE-06 merged the AST-identical `plan_edit_with_stats`/`plan_adapt_with_stats` —
but on the narration side the same block is still copied ~7 times in
`app/analysis/reports.py`: `run_compare` (:940), `run_wrapped` (:996),
`run_race_plan` (:1048), `run_insights` (:1125), `run_digest` (:835),
`run_activity_analysis` (:763) and (with variations) `run_analysis`. The skeleton
is identical:

```
key = _X_cache_key(context, MODEL_X)
cached = await llm_cache.get(session, key)
if cached: text, stats = cached, CallStats(cached=True)
else:
    try: text, stats = await _run_claude(x_with_stats, context, api_key)
    except AnalystError: await repository.log_report(..., ok=False); raise
    await llm_cache.put(session, key, text, CACHE_TTL_S)
await repository.log_report(..., ok=True, ...)
return text
```

Extract `_run_cached_narration(session, *, user_id, kind, model, context,
with_stats_fn, cache_key, question, max_...)` and leave each `run_*` as a thin
wrapper: "assemble context → check has_signal → call the engine". Do NOT touch
the `*_with_stats` signatures — the tests monkeypatch them (the CODE-06 lesson).
Cover the run_digest/run_analysis differences (extra question/report_text fields)
with parameters, not a fork.

### A2 · Generic cache-key builder

`_digest_cache_key`/`_insights_cache_key`/`_wrapped_cache_key`/`_race_cache_key`/
`_compare_cache_key` (`app/analysis/cache.py:132–215`) — one and the same shape:
pick fields from context + model + a kind marker → `sha256(json)`. A single
`_context_cache_key(kind: str, context: dict, model: str, fields: tuple)`
replaces all five; the "README pitfall" docstrings move to it. `_cache_key`/
`_ask_cache_key`/`_activity_cache_key` have their own logic — leave them alone.

### A3 · Bot-command decorator

~20 handlers in `bot/handlers.py` repeat the skeleton: `async with
async_session_maker()` → `_resolve_user` → early return → (optionally
`load_credentials`) → `try/except AnalystError → reply_text(str(e))` (12 copies
of the try/except). A decorator along the lines of `@bot_command(creds=True)`
that injects `(session, user, creds)` would cut 150–200 lines and make the
AnalystError handling guaranteed-uniform (right now the texts/logs drift slightly
between commands). Same idea CODE-04 applied to the jobs
(`for_each_user`/`user_garmin_runtime`) — now for the commands.

### A4 · CLI preamble

~10 commands in `app/cli.py` (all the `_backfill_*`, `_push_plan`, `_unpush_plan`,
`_fix_stride_paces`, `_convert_easy_hr`, …) repeat: `await init_db()` → session →
`users.get_by_email` → "User not found" → `user_runtime(session, user)` →
`run_in_threadpool(get_provider().login)`. One async context manager
`cli_user(email, *, garmin=False)` removes ~10 lines from each command and gives
the future OPS-01 login migration a single place to patch.

### A5 · Scattered micro-helpers

- `_mean` is defined three times: `app/injury.py:122`, `app/correlations.py:51`,
  `app/subjective.py:59`; next to `_avg`/`_median` in
  `app/garmin/repository.py:607`. One `app/statutil.py` (avg/median/mean) — and
  four modules import it.
- Date/pace formatters: `plan.py::_dow/_dm`, `bot/jobs.py::_dow_label`,
  `records.py::_fmt_pace`, `me.py::_pace_str` — candidates for a shared
  `app/format.py` (optional; lower value than the `_mean` cleanup).

Deliberately NOT touched: `fueling.estimate_minutes` vs `plan.py::_est_minutes` —
the duplicate is documented as intentional (a core module must not depend on a
web router).

---

## B. Oversized files

### B1 · `app/garmin/repository.py` — 1621 lines, ~60 functions

The project's largest file; effectively 6 domains in one namespace:
daily/activities, records, plans+workouts, reports/costs,
bot_state+pending-edits, window statistics (`window_stats`/`wrapped_stats`/
`weekly_*`). Split into a package `app/garmin/repository/` (`daily.py`,
`activities.py`, `plans.py`, `reports.py`, `state.py`, `stats.py`) with an
`__init__` facade re-exporting everything — exactly the CODE-01 recipe (external
imports `from app.garmin import repository` and the tests' monkeypatch paths keep
working, zero behaviour change).

### B2 · `bot/jobs.py` — 1118 lines

After CODE-04 the frame is shared, but the file mixes three independent layers:
the morning tick with 8 hooks (`_tick_for_user` + `_token_expiry/_records/
_injury/_health/_deload/_adapt_morning/_extend_nudge`), the weekly/monthly jobs
(digest, compare, insights, adapt), and the daily sync (plan_sync, race pack,
gear). A `bot/jobs/` package (`morning.py`, `weekly.py`, `sync.py`, `shared.py`
for `for_each_user`/`user_tz`/`_send_adapt_proposal`) with a facade `__init__` —
the same safe move.

### B3 · `tests/test_routers.py` — 1421 lines

One file for every router. Split into `test_routers_auth/plan/me/…` — purely
mechanical, and speeds up running a single slice locally.

`app/analysis/prompts.py` (957) is not a problem: it's prompt text, the size is
honest. `bot/handlers.py` (1171) and `reports.py` (1277) drop off this list by
themselves once A1/A3 land.

---

## C. Dead code

Verified by grepping `app`/`bot`/`tests` + vulture:

- **`app/analysis/reports.py:225 analyze()`** — zero callers (only the re-export
  in the `service.py:123` facade remains). Remove together with the re-export.
- **`app/garmin/service.py:311 build_payload()`** (synchronous) — called only
  from `tests/test_garmin_service.py`; the docstring says "CLI / fallback" but
  the CLI doesn't use it. Either honestly re-document it as "test harness only",
  or remove it and point the tests at `_fetch_days`.
- **`app/garmin/client.py:513 delete_schedule()`** — zero callers (unpush goes
  through `delete_workout`; deleting the workout drops its schedule too). Remove
  or document as API symmetry.
- **`app/analysis/client.py:34 SONNET_4_6`** — the constant is used nowhere (the
  price lives in `PRICES` as its own string key). One-line cleanup + the
  re-export.
- **The legacy `garmin_cache.json` → `.migrated` migration** in `client.py` —
  one-time; once it's confirmed the file on the Pi is already `.migrated`, the
  branch can go (low priority).
- **NOT dead** (leave alone): `gconn`/`providers.py` — the backbone of OPS-01's
  plan B (the ANALYSIS.md §0 verdict); the sync `*_with_stats` wrappers — the
  tests monkeypatch them; the ORM columns/routes vulture flags as "unused" —
  false positives.

### C2 · One-off CLI fix commands

`_fix_stride_paces` (~80 lines + `_parse_pace_ranges`/`_stride_pace_from_desc`/
`_strides_to_pace`) and `_convert_easy_hr` (~100 lines + `_convert_easy_steps`)
in `app/cli.py` are one-time data-fix utilities for a specific historical DB
state. If they've done their job — delete them (git history keeps them); if not —
move them to `scripts/` so `cli.py` holds only living commands. Together with A4
this shrinks `cli.py` from 942 to roughly ~500 lines.

---

## D. Optimizations

### D1 · Risk detectors computed up to 3× per tick

In `_tick_for_user` (every 20 min, 07–23 window): `_deload_check_for_user` calls
`build_injury_assessment` **and** `build_health_alerts`; when the deload doesn't
fire, `_injury_check_for_user` recomputes `build_injury_assessment` and
`_health_check_for_user` recomputes `build_health_alerts`. Each call is its own
set of 90-day history reads (`read_load_history`/`recent_subjective_runs`/
`read_history`/`count_daily_metrics`). Fix: compute both assessments once in
`_tick_for_user` and pass them down as parameters — the guard logic doesn't
change, and ⅔ of the redundant reads in the bot's hottest path disappear. As a
bonus the detectors themselves could be gated to the morning window (right now
they churn all day even though the DMs are guard-blocked anyway).

### D2 · `_weather_chips` — a live fetch on every `/plan` render

A documented v1 decision (Open-Meteo is free), but every GET of the page is a
network request in `run_in_threadpool`. A ~15-min TTL cache (in-process, keyed by
coordinates) removes the latency of repeated page opens. Minor — do it in
passing.

### Non-problems (checked, so they don't look suspicious)

- `run_analysis` reads the 90-day history once and shares it between `norm` and
  `health.detect` — already optimized (ST-10).
- The hot reads' indexes are in place (the PERF-03 slice); the dedup cache lives
  in the DB (PERF-02); the Claude pool is separate from anyio (PERF-04b).

---

## Suggested order, if picking this up

1. **A1 + A2** (one pass — both live in `analysis/`, the biggest line-count cut).
2. **C1 + C2** (cleanup, zero risk, an hour of work).
3. **D1** (small, but in the hottest path).
4. **A3, A4** (mechanical, large surface — separate PRs).
5. **B1, B2, B3** (file splits via the CODE-01 facade move — only after A*, so
   the duplicates aren't carried into the new files).
