# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ Cost safety — HARD RULE (do not skip)

**Never run anything that makes a real Anthropic API call without explicit, per-time
permission from the user.** Real calls cost real money — plan generation runs on **Opus
with `max_tokens=16000`**, the single priciest path. This covers: running the bot
(`python -m bot.main`), the web app (`uvicorn app.main:create_app`), API-calling CLI
(`app.cli push-plan`, `backfill-*`), any `/test_morning`/`/test_digest` path, and **any
ad-hoc script** that imports `app.analysis.service` / `build_payload_cached` with a real
`ANTHROPIC_API_KEY`.

- To exercise an LLM path, use the **mocked test suite** (`./venv/bin/python -m pytest`) —
  every Claude call is patched (`generate_plan_with_stats`, `run_plan_generation`,
  `analyze_with_stats`, …); the suite spends **$0**. A real call always logs
  `CLAUDE OK … ~$…` and writes a `report_logs` row — that's the audit trail (absence of
  that line = no real call happened).
- If the bot/web must run locally, ask first and run with an **empty/dummy
  `ANTHROPIC_API_KEY`** unless a real call is explicitly requested.
- Beware: a shell-exported `ANTHROPIC_API_KEY` also makes **Claude Code itself** bill
  per-token to that key (console usage) instead of the subscription — keep it out of the
  environment when not needed.

## What this is

A personal **Garmin → Claude** analyzer with a shared core reused by two front-ends:

- a **Telegram bot** (`bot/`) — commands + a scheduled morning report;
- a **FastAPI web layer** (`app/`) — JSON endpoints for reports, status, and history.

Both call the same services (`app.garmin`, `app.analysis`) over an async SQLAlchemy
database that stores history, caches immutable days, and tracks cost.

## Running

Always use the venv interpreter — the system Python is aliased and won't find the
installed packages:

```bash
# Install (editable, with dev extras):
./venv/bin/python -m pip install -e ".[dev]"

# Create / migrate the database (run once, and after model changes):
./venv/bin/python -m alembic upgrade head

# Start the web API (factory + lifespan):
./venv/bin/python -m uvicorn app.main:create_app --factory

# Start the Telegram bot:
./venv/bin/python -m bot.main

# Tests + lint:
./venv/bin/python -m pytest -q
./venv/bin/python -m ruff check app bot tests
```

The web app also runs zero-config: `init_db()` in the lifespan creates tables on
startup, so `uvicorn` works even before `alembic upgrade head`. Alembic remains the
source of truth for schema changes.

### First-run bootstrap (multi-user)

Credentials are now **per user, stored encrypted in the DB** — `.env` Garmin/Claude
values are only a seed source. After installing:

```bash
# 1. Generate a master key and put it in .env as APP_SECRET_KEY:
./venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 2. Migrate (adds users + user_id columns):
./venv/bin/python -m alembic upgrade head

# 3. Create the first admin, seeding its creds from .env and claiming existing data:
./venv/bin/python -m app.cli create-user --email me@example.com --admin --seed-env
```

Then log in at `/login`, manage credentials at `/settings`, add users at `/admin/users`.

## Environment

`.env` (read by `app.core.config.Settings` via pydantic-settings):

```
APP_SECRET_KEY=        # Fernet key — encrypts creds + signs sessions (required for auth)
TELEGRAM_BOT_TOKEN=    # the (single) bot identity — global
# Seed-only (per-user after bootstrap; used by `create-user --seed-env`):
GARMIN_EMAIL=
GARMIN_PASSWORD=
ANTHROPIC_API_KEY=
TELEGRAM_CHAT_ID=
```

Optional, with defaults:

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_SECRET_KEY` | `` (empty) | Fernet master key: encrypts stored creds + signs cookie sessions. **Empty → sessions signed with an ephemeral per-process key** (SEC-01): a loud `AUTH: APP_SECRET_KEY is not set` error + a `/login` banner; sessions don't survive a restart but can't be forged (a fixed fallback let anyone forge an admin cookie). Credential encryption still hard-requires it (`crypto` fails without it). |
| `LOGIN_RATE_LIMIT` | `5` | SEC-01: max `POST /login`/`POST /register` attempts per window before a 429; `0` disables (tests set it to 0). In-memory + per-process (`app/core/ratelimit.py`) — a single Pi web process, by design. |
| `LOGIN_RATE_WINDOW_S` | `300` | SEC-01: the rate-limit window in seconds. |
| `GARMIN_PROVIDER` | `garth` | Garmin backend: `garth` (working) or `gconn` (untested) |
| `GARTH_TOKEN_DIR` | `~/.garth` | Legacy global garth token dir (per-user tokens live in the DB) |
| `GARMIN_RPS` | `3.0` | Process-wide Garmin request rate cap (req/s); `0` disables the limiter (PERF-05) |
| `GARMIN_RETRIES` | `2` | 429 retries with exponential backoff inside `client._api` (PERF-05) |
| `CLAUDE_MAX_WORKERS` | `4` | Size of the dedicated Claude thread pool, off the shared anyio pool (PERF-04b) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
| `WEB_TOKEN` | `` (empty) | Legacy shared secret; superseded by login (kept for compatibility) |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root level (`DEBUG` shows skip-reason logs) |
| `GARMIN_CACHE_DIR` | `garmin_cache` | Per-key disk cache for immutable Garmin assets (PERF-02) |
| `GARMIN_CACHE_FILE` | `garmin_cache.json` | Legacy single-file cache — seeded into `GARMIN_CACHE_DIR` once, then renamed `.migrated` |
| `INJURY_RADAR` | `True` | NF-04: master on/off for the injury-risk advisory in the morning tick |
| `INJURY_MIN_HISTORY_DAYS` | `14` | NF-04: quiet calibration — no warnings until this much daily history |
| `INJURY_GUARD_DAYS` | `5` | NF-04: at most one injury advisory per this many days |
| `HEALTH_ALERTS` | `True` | EP-08: master on/off for proactive recovery-anomaly alerts in the morning tick |
| `HEALTH_MIN_HISTORY_DAYS` | `7` | EP-08: cold-start gate — no alert until this much daily history |
| `HEALTH_ALERT_COOLDOWN_DAYS` | `3` | EP-08: same alert kind at most once per this many days (per-rule cooldown) |

`CLAUDE_CACHE_FILE` is gone — the Claude dedup cache lives in the `llm_cache` table
(PERF-02), shared by the bot and web processes.

`STATE_FILE` is gone — the morning-sent date lives in the `bot_state` table, per user.

## Authentication & multi-user

- **Users**: `users` table (login email + bcrypt hash, `is_admin`, encrypted
  Garmin/Claude creds + garth token, plaintext indexed `telegram_chat_id`, a
  `weather_location`/`latitude`/`longitude` for the morning weather lookup, and the
  per-user feature toggles `garmin_sync_enabled`/`plan_adapt_enabled`/`alerts_enabled`
  — the last governs EP-08 health alerts). Web login is a signed cookie session
  (`SessionMiddleware`, signed by `APP_SECRET_KEY`).
- **Secrets**: `app.core.crypto` — Fernet encrypt/decrypt for creds, bcrypt for
  passwords. `app.garmin.credentials.load_credentials` decrypts a user into a runtime
  `UserCredentials`.
- **Web-login hardening (SEC-01)**: `POST /login` (keyed per-IP + per-email) and
  `POST /register` (per-IP) are rate-limited by an in-memory sliding-window
  `RateLimiter` (`app/core/ratelimit.py`) — per-process on purpose (single Pi web
  process; a restart resets counters, a second process wouldn't share them; do NOT
  "fix" that into global state). Over the limit → 429 with a human message.
  `LOGIN_RATE_LIMIT`/`LOGIN_RATE_WINDOW_S` tune it (`0` disables — the test suite
  sets it to 0 in `conftest`; dedicated rate-limit tests build their own limiter).
  A missing `APP_SECRET_KEY` no longer falls back to a constant secret (that let
  anyone forge an admin session) — `create_app` logs `AUTH: APP_SECRET_KEY is not set`
  and signs sessions with an **ephemeral per-process** Fernet key (sessions die on
  restart, but are unforgeable), plus a `/login` banner. `/logout` is **POST** (a
  form-button in `_nav.html`) so a cross-site `<img src=/logout>` can't sign you out;
  the old `GET /logout` is a stateless redirect to `/settings`. `same_site="lax"`
  already blocks cross-site form POSTs, so no CSRF tokens at this stage.
- **Per-user runtime**: `app.garmin.runtime.user_runtime(session, user)` binds that
  user's Garmin provider (a `garth.Client` resumed from the stored token, else
  email+password login — saving a fresh token) via a ContextVar, and yields
  decrypted creds (so `run_analysis(..., api_key=creds.anthropic_key)` uses their key).
  All data reads/writes are scoped by `user_id`. A login that hits Garmin's MFA gate
  raises `MFARequired` rather than hanging or silently failing — `user_runtime` lets it
  propagate (see the MFA re-login bullet below).
- **Remote MFA re-login**: the installed `garth` (0.4.47) has no `return_on_mfa`/
  `resume_login` pair — `Client.login(email, password, prompt_mfa=...)` just blocks on
  a callback until it returns a code. `app.garmin.mfa` bridges that into a two-request
  web flow: `start_login` runs the real `login()` on a background thread whose
  `prompt_mfa` parks on a `queue.Queue`; the initiating call waits up to ~25s for either
  a fast (no-MFA) result or the MFA gate. On the gate it raises `MFARequired(user_id)`
  and leaves the thread parked (module-level `_pending` dict, in-memory, TTL ~10 min —
  deliberately per-process, since an MFA trigger from the bot can't be finished there).
  A follow-up `submit_code(user_id, code)` delivers the code into the same paused
  thread and returns the fresh token. On `/settings`, `POST /settings/garmin-connect`
  kicks off `start_login` for the current user (the only way to actually reach the MFA
  gate — it must run in the web process); if it raises `MFARequired`, `GET /settings`
  shows a code-entry form (`garmin_mfa_pending` via `mfa.has_pending`) whose
  `POST /settings/garmin-mfa` calls `submit_code` and persists the resulting token.
  A wrong/expired code clears the pending state so the user just retries the whole
  connect step. The bot's global `on_error` and the morning job's `_tick_for_user`
  both catch `MFARequired` and send a friendly "finish the login in /settings" message
  instead of a generic error (the morning job guards it to once/day via `bot_state`,
  key `mfa_notified:<date>`); a top-level FastAPI `exception_handler(MFARequired)` in
  `app.main` covers the JSON endpoints (`/report.json`, `/deep`, …) with a 409.
  Changing Garmin email/password in `/settings` cancels any pending MFA state (it was
  for the old creds). Normal token-resume and no-MFA logins are unaffected — the bridge
  only engages once Garmin actually asks for a code.
- **Registration**: `/register` is public — a self-signup creates an unapproved,
  non-admin user (`is_approved=False`) that **cannot log in** until an admin approves
  it at `/admin/users` (approve / delete buttons). Admin- and CLI-created users are
  approved on creation. Login lands admins on `/ui`, others on `/settings`.
- **Routes**: `/login`, `/logout`, `/register`, `/settings` (own creds +
  `POST /settings/password` to change password, verifying the current one),
  `/admin/users` (admin: list/create/approve/activate-deactivate/delete), `/me`
  (each user browses **their own** daily_metrics/activities/report_logs, with the
  HRV/sleep charts), `/ui` (raw DB browser — **admin only**). `/health` stays public;
  `/status`, `/report.json`, `/deep`, `/history` require login and act on the current
  user. The `/me` and `/ui` browsers share `index/table/detail.html` via a `base` var
  (`app.routers.me` is user-scoped; `app.routers.admin` spans all rows).
- **Active flag**: `is_active` (default True) is a separate admin off-switch from
  approval — a deactivated user keeps its data but can neither log in (403 "Акаунт
  деактивовано") nor receive bot reports (`_resolve_user` and `morning_job` require
  active + approved). Admins can't deactivate themselves.
- **Bot**: one global `TELEGRAM_BOT_TOKEN`; an incoming chat is mapped to a user by
  `telegram_chat_id` (`_resolve_user`). `morning_job` loops over every user with a
  chat id + Garmin creds, each guarded once-a-day via per-user `bot_state`.
- **CLI**: `python -m app.cli create-user [--admin] [--seed-env]` — `--seed-env`
  encrypts `.env` creds into the user and claims pre-existing (unowned) data rows.
  `import-garth-token --email` seeds a user's garth session from `~/.garth`;
  `backfill-series --email` fetches the pace/HR series for already-stored runs that
  predate the feature (fills nulls only, idempotent). `import-export --email --path
  [--since YYYY-MM-DD] [--overwrite]` backfills `daily_metrics` (+`extra`) **and**
  `activities` from a Garmin GDPR export folder offline (no API → no 429).
  `app.garmin.export_import` merges the per-date JSON (sleep, UDS daily summary,
  healthStatus for HRV, VO2max, race predictions, endurance, readiness) — the export uses
  different keys/units than the live API (sleep score = `overallScore`; HRV in
  `healthStatusData.metrics`; all-day stress avg+max in `allDayStress.aggregatorList`;
  activity distance in cm). Existing days are **merge-filled** (only NULL columns + missing
  `extra` keys; `--overwrite` is still null-safe — never writes a null over a value);
  activities insert only ids not already stored (summary only — no pace/HR series).
  `/history` caps at 365 days, so `--since` ~1y is plenty. `import-fit-series --email
  --path [--since]` then fills runs' pace/HR `series` from the export's **FIT files**
  (`DI-Connect-Uploaded-Files`, parsed with `fitparse`) — no API: it scans the FIT files,
  matches each activity FIT to a run by its session start time (== the activity
  `beginTimestamp`; the FIT filenames are *not* activity ids), and downsamples the
  records to the same `[{d, p, hr}]` shape as the live `/details` path. **JSON-null
  gotcha**: a JSON column stores Python `None` as JSON `null` (not SQL NULL), so
  "series is missing" is filtered in Python, not via `series.is_(None)` (same in
  `backfill-series`). `push-plan --email [--days 14] [--dry-run]` is the reverse
  direction: it **writes** the active plan's upcoming `PlannedWorkout`s to the Garmin
  Connect calendar (a rolling window like Runna — only `planned` runs in the next
  `--days`, skipping rest/cross). `app.garmin.workout_export.build_workout` converts our
  `steps` (`warmup/run/recovery/repeat` + `pace_min_km [fast, slow]`) into Garmin's step
  DTOs — pace becomes a `pace.zone` target with `targetValueOne/Two` as **speed in m/s**
  (`1000/(min_km*60)`; One = faster bound), distance/time map to `endCondition`
  distance(metres)/time(s), and `repeat` → `RepeatGroupDTO` with continuous `stepOrder`.
  `client.create_workout`/`schedule_workout`/`delete_workout`/`delete_schedule` are the
  POST/DELETE calls. Workout names carry a per-type emoji (`workout_export._TYPE_MARK`:
  🌿 easy / 🗻 long / 🔥 tempo / ⚡ intervals …) so they read at a glance and are visibly
  not Runna's. Each pushed session records `garmin_workout_id`/`garmin_schedule_id` on the
  row so re-runs are **idempotent** (skip what's already there). `--dry-run` builds +
  prints the payloads without writing; `--date YYYY-MM-DD` targets one session;
  `unpush-plan --email [--date]` removes pushed workouts (by stored id; tolerant of a
  workout already deleted in the UI — never touches manual/Runna workouts).
  `token-expiry` (OPS-01) decodes every user's stored garth token — OAuth1 issue date
  (= the OAuth2 JWT `iat`, since we persist only right after a fresh login) and the
  ≈+1y death date, i.e. each user's auth deadline (`app/garmin/token_info.py`;
  read-only raw SQL so it works even on a half-migrated DB).
- **Live calendar sync** (`app.garmin.plan_sync.sync_plan_to_garmin`): the automated
  rolling-window keeper (the CLI's manual cousin). Two passes — **forward** (push the
  active plan's upcoming in-window unpushed runs) and **cleanup** (remove anything we
  pushed that's now stale: past date, non-`planned` status, or belonging to a plan that's
  no longer active — i.e. archived/regenerated). Cleanup keys off
  `repository.list_pushed_workouts` (all of a user's pushed rows) vs the active plan id.
  Run from three hooks: a **separate daily bot job** (`bot.jobs.plan_sync_job`, scheduled
  via `JobQueue.run_daily` at `PLAN_SYNC_HOUR` Europe/Warsaw — deliberately **not** in
  `morning_job`, which is a different concern and fires every 20 min), the
  **`/plan/archive`** route (immediate
  unpush of the archived plan), and **background plan generation** (`_generate_plan_bg`,
  to swap the old plan's calendar for the new one). All hooks bind `user_runtime` and are
  best-effort (a Garmin outage never breaks the action; the daily job reconciles later).
  `push_workout`/`remove_workout` are the shared one-session helpers reused by the CLI.
  A **bot plan edit** (`/plan <text>` → confirm) re-syncs **only the touched sessions** via
  `plan_sync.resync_workouts` (in `plan_callback`): `apply_plan_ops` now returns the
  affected `PlannedWorkout`s, and each gets its old Garmin copy dropped + re-pushed if it's
  still an upcoming in-window run (a `move` lands on the new date, `skip`/past just get
  removed) — the cheap per-edit diff, with the daily job as the full backstop.
- **Sync toggle** (`User.garmin_sync_enabled`, default on): a per-user master switch. All
  four **automatic** hooks (daily job, archive, generation, edit) check it and skip when
  off; the **manual** `push-plan` CLI ignores it (explicit override). Set on the plan-setup
  form (a checkbox — uncheck to generate + validate a plan without touching the calendar)
  and in `/settings`. Flipping it in settings applies immediately (best-effort):
  on → `sync_plan_to_garmin` (push the window), off → `plan_sync.unpush_all` (clear
  everything we pushed).
- **Strength sessions** (opt-in): the plan-setup form has a **per-weekday picker** — a
  dropdown for each day of the week choosing one of the user's saved Garmin strength
  workouts (Day 1/Day 2, fetched by `client.fetch_workouts` / `plan.py:_strength_workouts`),
  **"🆕 інше…"** (a free-text session generated from scratch — reveals a description input),
  or "— нема —". The form posts `strength_<slug>` (id | `"custom"` | "") + `strength_desc_<slug>`
  → `intake["strength"]` with `assignments` (`{weekday_slug: workout_id}`) for saved picks and
  `custom` (`{weekday_slug: description}`) for free-text ones. On generation,
  `run_plan_generation` turns each distinct description into a `StrengthSession` via
  `generate_strength_with_stats` (`SYSTEM_STRENGTH_GEN`, deduped, sanitised by
  `_sanitize_strength`) and lays it as a from-scratch `strength_plan` day (built natively on
  push — the same path as chat "додай силову"; renders in the `/plan` accordion with no
  snapshot needed).
  **Preview (ST-05)**: each "🆕 інше…" description has a **"Прев'ю"** button that POSTs to
  `/plan/strength/preview` (`plan.py::strength_preview` → `service.run_strength_preview`) —
  the **same context/model** as generation, so the previewed session matches what generation
  would produce. It logs a `ReportLog(kind="strength")` (cost visible) and returns the
  `_strength_preview.html` fragment (same accordion look), carrying the sanitised session +
  a `_desc_hash(description)` in `data-session`/`data-hash`. On submit those ride back as
  hidden inputs `strength_preview_<slug>`/`strength_prehash_<slug>`; `_confirmed_previews`
  keeps only sessions whose hash still matches the submitted text (an **edited description
  invalidates** its stale preview) and **re-sanitises** every one server-side (never trust
  the client JSON) into `intake["strength"]["custom_generated"][slug]`. `run_plan_generation`
  reuses a confirmed session verbatim (skips the second, paid Claude call) — falling back to
  `generate_strength_with_stats` only for descriptions with no confirmed preview. The button
  is progressive enhancement: without JS the form works exactly as before (preview is optional).
  `repository.add_strength_workouts(plan, assignments, snapshots, custom)`
  lays `PlannedWorkout(type="strength", garmin_template_id=<saved id>)` on
  each chosen weekday **every week** — a **fixed** day→workout pairing (no rotation, so Day 1
  always falls on the same weekday). On push, a session with a `garmin_template_id`
  is **cloned** (`client.fetch_workout_full` + `workout_export.clone_workout` strips ids,
  keeps exercises, names it `🏋️ Day X · Wn`) into **our own** copy which is then scheduled —
  so the user's reusable Day 1/Day 2 templates are never scheduled or deleted (cleanup only
  removes our copy). `plan_sync._pushable` lets strength-with-template past the run-only
  filter. `list-workouts --email` prints saved workout ids.
- **Strength exercise snapshot for the plan view**: a clone day's exercises live in the
  Garmin **template**, not our DB — so to render the `/plan` exercise accordion without a
  fetch-per-render, `run_plan_generation` snapshots each chosen template once at build time
  (`client.fetch_workout_full` → `workout_export.read_exercises`) into
  `PlannedWorkout.strength_snapshot` (JSON `{name?, exercises:[{category, exercise?, reps?}]}`,
  **display-only** — never used on push; the real template is still cloned live).
  `plan.py:_strength_details` builds the accordion from `strength_plan` (from-scratch),
  `strength_snapshot` (clone, from the DB), and only **falls back to a live template fetch**
  for clone days on plans made before snapshots existed (best-effort; Garmin outage → the
  page renders without the exercise list). `plan.html` renders `strength_view[w.id]` blocks
  and shows the snapshot's real name in place of "Силова".
- **Plan-generation model toggle**: the setup form has an Opus/Fable radio (`plan_model`
  slug → `service.resolve_plan_model` → `PLAN_GEN_MODELS`; default `MODEL_PLAN_GEN`=Opus,
  alt `MODEL_PLAN_GEN_ALT`=`claude-fable-5`). The chosen id flows through `params["model"]`
  → `run_plan_generation(..., model=)` → `generate_plan_with_stats(..., model=)`. `PRICES`:
  Opus 4.8 $5/$25, Fable 5 $10/$50 per 1M in/out (Anthropic list price) — so Fable is **2×
  Opus**; the form shows both prices so the choice is cost-aware (Opus is the cheaper default).
- **Editing exercises in a strength day** (chat): «заміни гіперекстензію на станову тягу» →
  `SYSTEM_PLAN_EDIT` emits a `swap_exercise` op (`from_category`/`to_category`/`exercise`/
  `reps`), mapping the UA name to a **Garmin category code**. The valid codes come from
  `app.garmin.exercises` (Garmin's own taxonomy in `exercise_catalog.json` — top-level
  category → exercise variants; a built-in `_FALLBACK_CATEGORIES` set keeps validation
  working before the file is dropped in; `exercise_types.properties` gives display labels
  via `label()`). `run_plan_edit` feeds `exercises.CATEGORIES` as the vocabulary so the
  model can't invent a code; `repository.apply_plan_ops` **validates** `to_category`
  against the catalog (rejects hallucinated codes) and appends the swap to
  `PlannedWorkout.exercise_edits` (JSON list of `{from,to,exercise,reps}`). On push,
  `workout_export.apply_exercise_edits` mutates the **cloned** template DTO — finds the
  step whose `category` matches `from`, swaps `category`/`exerciseName`/`reps` (recurses
  into repeat groups; weight left alone — unit unverified) — so the swap lands on the
  watch (via `plan_sync.resync_workouts`, remove+repush of just the touched day).
- **Generating a strength day from scratch** (chat, free-text focus): «додай силову на ноги»,
  «згенеруй силову на верх», «зроби силову як Day 1 але на ноги» → `SYSTEM_PLAN_EDIT` emits an
  `add` (type=strength) carrying a **`strength`** object (`schemas.StrengthSession`:
  `{name, warmup_s, blocks:[{reps=sets, rest_s, exercises:[{category, exercise, reps,
  weight_kg}]}]}`) — the model **builds the session itself** (no template clone).
  `apply_plan_ops` runs it through `repository._sanitize_strength` (keeps only exercises whose
  `category` is a real Garmin code from `exercises.CATEGORIES`, drops empty blocks — a
  hallucinated code never reaches the watch) and stores it on `PlannedWorkout.strength_plan`
  (JSON). On push, `plan_sync.push_workout` sees `strength_plan` **first** (before
  `garmin_template_id`) and calls `workout_export.build_strength_workout` → a native Garmin
  strength DTO (sportType 5; repeat-group blocks of `interval`(reps) exercises + a trailing
  rest, lap-button rests between groups, `weightValue` in **kg** / `-1.0` = bodyweight,
  continuous `stepOrder`). The DTO shape was verified field-for-field against a real saved
  strength workout and validated live on the watch. `run_plan_edit` always feeds
  `exercise_categories` (so generation works even when the plan has no strength day yet) plus
  `strength_templates[].exercises` (`read_exercises`) as a structural seed for "similar to
  Day 1/2" requests. `_pushable` allows a `strength_plan` session. The older **clone** path
  (`garmin_template_id` + `swap_exercise` → `exercise_edits`) is kept only for scheduling a
  saved Day 1/Day 2 **as-is** or editing such a cloned day. **Not yet**: AI progression on the
  exercises (weights/sets over the plan), a setup-form surface for generation (chat-only).

## Structure

```
app/
  main.py              create_app() factory; SessionMiddleware; RequiresLogin→/login; routers
  cli.py               admin CLI: create-user [--admin] [--seed-env]
  core/
    config.py          pydantic-settings Settings — the single source for all env vars
    logging.py         logging config (was logging_setup.py)
    security.py        verify_token dependency (legacy WEB_TOKEN; superseded by auth.py)
    crypto.py          Fernet encrypt/decrypt for creds + bcrypt password hashing
    auth.py            current_user / require_admin deps; session login/logout helpers
  db/
    base.py            async engine + sessionmaker + declarative Base; init_db/dispose_db
    session.py         get_session() request dependency
    models.py          ORM: User, DailyMetric, ActivityRecord, ReportLog, LlmCache, BotState (user-scoped)
    users.py           user queries: get_by_email / get_by_chat_id / create_user
    llm_cache.py       async get/put over llm_cache — the cross-process Claude dedup cache
  garmin/
    providers.py       legacy global + _UserGarthProvider + provider ContextVar
    credentials.py     load_credentials(user) → decrypted UserCredentials
    runtime.py         user_runtime(session, user): bind provider, persist fresh garth token
    client.py          low-level connectapi fetches + disk cache for immutable assets
    service.py         aggregation; build_payload (sync) + build_payload_cached (async, per-user)
    repository.py      user-scoped upserts/reads, ReportLog, per-user BotState
    schemas.py         Pydantic Payload / DailySummary / Activity / PlannedRun
    exercise_names.py  Garmin exercise NAME codes → readable Ukrainian
  weather.py           Open-Meteo geocode (settings) + today's forecast (morning report)
  analysis/
    service.py         analyze/ask/run_analysis/run_ask; per-key Anthropic client; dedup cache
    prompts.py         SYSTEM + SYSTEM_ASK prompts
  routers/
    auth.py            GET/POST /login, GET /logout
    settings.py        /settings (own creds), /admin/users (admin)
    health.py          GET /health (public), GET /status (login, per-user)
    reports.py         GET /report.json (Sonnet), GET /deep (Opus) — login, per-user
    history.py         GET /history?days=N — trends from DB, login, per-user
    plan.py            GET/POST /plan — training-plan setup form + view, login, per-user
    admin.py           /ui DB browser — admin only
  dependencies.py      shared deps (get_session, verify_token)
bot/
  main.py              builds the Application, registers handlers + job, run_polling
  handlers.py          /report, /ask, /deep, /activities, /activity, /records, /risk, /health, /plan (+edit), /test_*; _resolve_user, error handler
  jobs.py              morning_job loops users (Europe/Warsaw window; per-user once-a-day guard)
alembic/               migrations (async env.py wired to Base.metadata + DATABASE_URL)
tests/                 pytest: crypto, garmin service, routers (login), repository, user runtime
```

## Architecture and data flow

```
Telegram command (chat_id→user) / HTTP request (session→user)
  → async with user_runtime(session, user) as creds:   # binds user's garth provider
      → service.build_payload_cached(session, user.id, days, activity_limit)   [async]
          → provider.login() (per-user garth.Client; token resumed/persisted in DB)
          → past immutable days served from DB (repository.read_daily_metrics, user-scoped)
          → today + missing days fetched via Garmin (run_in_threadpool); activities, planned
          → persist_payload(): upsert daily + activities (idempotent, per user)
          → typed Payload (synced_today, last_data_date, daily[], recent_activities[], planned_runs[])
      → analysis.run_analysis(session, payload, user_id=…, api_key=creds.anthropic_key, …)
          → dedup cache check (hash of payload+date+question+model) — early return on hit
          → Sonnet (/report, morning) or Opus (/deep); AnalystError → user-visible message
          → ReportLog row written (user_id, tokens, cost, ok/error)
  → reply / JSON response
```

The aggregation in `app/garmin/service.py` is the cost-control layer — raw Garmin
responses are collapsed to ~12 fields/day and never sent to the LLM.

## Web endpoints

- `GET/POST /login`, `GET /logout`, `GET/POST /register` — cookie-session auth +
  self-registration (new users await admin approval before they can log in).
- `GET /health` — liveness (public, no auth).
- `GET /status` — the logged-in user's Garmin auth, DB stats, last morning report, cost.
- `GET /report.json` — daily report (Sonnet). Login; current user.
- `GET /deep?q=...` — deep analysis (Opus). Login; current user.
- `GET /history?days=N` — HRV/sleep/stress/body-battery trend from the DB. Login; current user.
- `GET/POST /plan` — training-plan setup form (no active plan) / plan view; `POST /plan/archive`
  (archive active), `GET /plan/archive` (list archived), `GET /plan/{id}` (read-only view of
  a past plan). Login; current user.
- `GET /settings` — manage own Garmin/Claude/Telegram creds (encrypted on save).
- `GET /admin/users` — list/create users (admin only).
- `GET /ui` + `GET /ui/{table}` + `/ui/{table}/{id}` — raw DB browser (whitelisted
  tables: users, daily_metrics, activities, report_logs, bot_state). **Admin only.**
  Templates in `app/templates/`.

Auth: a signed cookie session set at `/login` (no token headers). `current_user`
gates user endpoints; `require_admin` gates `/ui` and `/admin/users`. `WEB_TOKEN` is
legacy and no longer used by these routes.

## Database

- **Stack**: SQLAlchemy 2.0 async + Alembic. SQLite (`aiosqlite`) by default for
  zero-config on a Raspberry Pi; switch to Postgres (`asyncpg`) by setting
  `DATABASE_URL` only — no code changes.
- **Models**: `DailyMetric` (unique `date`, + `extra` JSON of unmodeled scalars),
  `ActivityRecord` (unique `activity_id`,
  `exercises` JSON + `series` JSON — per-point pace/HR for runs + `analysis` text —
  Claude's `/activity` writeup), `ReportLog` (cost/metrics + `question`/`report_text`),
  `BotState` (key/value), `TrainingPlan` (goal/params/intake/summary, one active per
  user) + `PlannedWorkout` (dated session: type/dist/description/status + `steps` JSON —
  structured warmup/run/recovery/cooldown/repeat breakdown with pace ranges, for richer
  detail and a future Garmin-Connect workout export), `PersonalRecord` (EP-14: one row per
  beaten best — `kind`/`value`/`previous_value`/`activity_id?`/`date`, history not just current).
- **DB as cache**: past days already stored are served from the DB instead of
  re-hitting Garmin; today is always refetched (still syncing). `build_payload_cached`
  persists what it fetches, so history accumulates.
- **Migrations**: `./venv/bin/python -m alembic upgrade head`. To add a migration after
  changing models: `./venv/bin/python -m alembic revision --autogenerate -m "msg"`.
  **On the Pi, back up first** — use `scripts/migrate.sh` (backs up, then upgrades) or
  run `scripts/backup_db.py` by hand before a bare `alembic upgrade head`; a failed
  migration on the live DB is the second most likely way to lose data.
- **Backups (OPS-02)**: `scripts/backup_db.py` makes an online-consistent copy of the
  SQLite DB (`VACUUM INTO`, not `cp` — the bot/web are still writing) to
  `backups/garmin-YYYY-MM-DD.db`, rotating 7 daily + 4 weekly. The DB path comes from
  `settings.DATABASE_URL` (Postgres would use `pg_dump` instead — out of scope).
  `deploy/systemd/garmin-backup.{service,timer}` runs it nightly; `--rsync-dest` copies
  the fresh backup **off the SD card** (an SD failure kills the DB and any backups
  beside it). The Fernet-encrypted creds in the DB are worthless without
  `APP_SECRET_KEY`, so the DB copy is safe to store anywhere — but a restore can't
  decrypt creds unless `APP_SECRET_KEY`/`.env` is backed up **separately** (password
  manager / encrypted file), which must be done once, out of band.
- **Index audit (PERF-03 slice)**: hot user-scoped reads have composite indexes —
  `activities(user_id, date)`, `report_logs(user_id, created_at)`,
  `planned_workouts(plan_id, date)`; `daily_metrics(user_id, date)` is already covered
  by its unique constraint. The Postgres switch itself stays frozen (tied to `/register`).

## Key design decisions

**Garmin provider**: `garth` is the working path (unofficial endpoints, token at
`~/.garth`, first run needs interactive MFA). A `gconn` provider over `garminconnect`
exists behind `GARMIN_PROVIDER=gconn` but is **untested against the live API** — do
not rely on it. Endpoint URLs and the m/s→min/km pace conversion are unchanged.
**Auth plan B (OPS-01)**: garth is deprecated upstream (Cloudflare TLS-fingerprinting;
the 0.4.47 pin still works — don't touch it), so auth failures are monitored via
grep-stable markers — `GARMIN AUTH FAIL` (ERROR, fresh login failed — the migration
trigger; logged in `mfa.start_login`, the single chokepoint for all fresh logins) and
`GARMIN AUTH: stored token resume failed` (WARNING, `_UserGarthProvider`). The
migration plan + a standalone recon script (`scripts/ops01_recon_gconn.py`, run in a
throwaway venv with the latest `python-garminconnect`) live in
`docs/backlog/OPS-01-garmin-auth-plan-b.md`.

**HRV is the primary recovery signal** — `hrv_status = BALANCED` means recovered; a drop is
the main stress indicator. (The dedicated resting-HR endpoint 403s via garth, but RHR comes
free inside the sleep DTO — stored in `extra.resting_hr`; see below.)

**`DailyMetric.extra` (JSON)** — everything we fetch but don't model as a typed column,
kept as a compact scalar dict (no per-minute arrays). Built by `service._daily_extra` from
the sleep DTO (RHR, overnight HRV, body-battery change, skin-temp deviation, SpO2,
respiration, restless moments, sleep need/feedback), the HRV summary (weekly avg, 5-min
high, baseline band, feedback) and **Training Readiness** — the one extra fetch
(`client.fetch_training_readiness`, `/metrics-service/metrics/trainingreadiness/{date}`):
`readiness_score`/`level`/`feedback`, `recovery_time_h`, `acute_load`, and the ACWR
(acute:chronic load) `acwr_pct`/`acwr_feedback`. `_daily_extra_metrics` adds the rest from
four more endpoints (all keyed by the **displayName**, not the email — the earlier 403 was a
wrong-identifier bug): **user summary** (`fetch_user_summary` — steps, distance, calories,
moderate/vigorous intensity minutes, floors, min HR, body-battery high/low), **VO2max**
(`fetch_vo2max`), **race-time predictions** (`fetch_race_predictions` — 5K/10K/half/marathon
seconds) and **endurance score** (`fetch_endurance`). Persisted (in `_DAILY_FIELDS`) and
served from the day cache. Used by the reports: not yet; used by plan generation: **yes** —
`run_plan_generation` feeds `SYSTEM_PLAN` three calibration inputs: (1) a `fitness` snapshot
coalesced from the last ~21 days of `extra` via `repository.get_recent_extra` (most-recent
non-null per key, since metrics refresh at different cadences) — VO2max + fitness age, race
predictions, endurance score/class, **training-load & injury risk** (ACWR %/feedback, acute
load, recovery time, readiness) and **recovery baselines** (HRV band, resting HR, SpO2,
respiration); (2) `weekly_volume` — running km/longest per ISO week over ~8 weeks
(`repository.weekly_run_volume`) as the anchor for ~10%/week progression; (3) the
`recovery` trend (now incl. `resting_hr`). The prompt eases volume / inserts deloads when
ACWR is high, recovery time long, RHR drifts up or HRV drops below its baseline band.

**Sync awareness**: `synced_today` / `has_data` / `last_data_date` distinguish "watch
hasn't synced" from "bad recovery." The morning job runs ~10s after startup, then every
20 min; the Europe/Warsaw window (07–12) and once-a-day guard live inside `morning_job`,
which logs its decision. The once-a-day guard persists in `bot_state`.

**Weather (morning report)**: if a user set a location in `/settings`, the morning job
fetches today's forecast (`app/weather.py` → Open-Meteo, no API key) and passes it to
`run_analysis(..., weather=...)`. `app.weather.geocode` resolves the typed city to
lat/lon **once on settings save** (stored on the user) so the morning job needs no
geocoding; `fetch_forecast` returns a compact today dict (min/max + feels-like, precip
mm/prob, max wind, a short Ukrainian condition, and six daytime hourly slots for
run-timing advice). Both helpers are network-bound and return `None` on any error, so a
weather outage never blocks the report. `weather` is part of the dedup-cache key and the
`SYSTEM` prompt instructs the analyst to factor heat/rain/wind into advice **only when a
run is today/tomorrow** (same proximity rule as pace detail). Wired into the morning job
only (not on-demand `/report`); `analyze_with_stats`/`run_analysis` take the param
generically so adding it elsewhere is trivial. The send path is factored into
`jobs._deliver_morning` (payload → weather → analysis → send), reused by both the
scheduled `morning_job` (with the time-window + once-a-day guard) and
`force_morning_for_user` (no guards) — the hidden bot commands `/test_morning` (one-shot)
and `/test_on` (repeating) call the latter, so a test exercises the **exact** morning path
incl. weather, without consuming the day's guard.

**Weekly digest (EP-07)**: a Sunday-evening retrospective per user with a chat id —
`bot.jobs.weekly_digest_job` (`run_daily` at `DIGEST_HOUR` Europe/Warsaw, `days=(DIGEST_WEEKLY_DOW,)`
= Sunday; scheduled just before the adaptive review, a *different* message: dessert recap vs
forward-looking correction). `analysis.service.run_digest` assembles the week's numbers **in
Python** (`_week_volume_summary` — this-week vs last-week km/runs/longest from
`weekly_run_volume`; `weekly_compliance` for plan/fact; `read_history` recovery trend;
`_build_fitness_snapshot` for VO2max/race-predictions; goal + `days_to_target`) and Sonnet
(`SYSTEM_DIGEST`, `MODEL_DIGEST`) only narrates + gives an honest progress-to-goal read
(explicit "відстаєш" when compliance < ~70%). No active plan → a shortened version (volume +
trends, no plan/fact). Returns `None` (nothing sent) for a user with no history and no plan.
Dedup-cached on the ISO week + data slice (not `today`, so a repeat within the week is a hit —
the README pitfall) and logged as `ReportLog(kind="digest")`. Guarded once/week via `bot_state`
(key `digest:<iso-week>`, set only after a message goes out). The send path is
`jobs._deliver_digest`, reused by the scheduled `_digest_for_user` (with the guard) and
`force_digest_for_user` (no guard) — the hidden `/test_digest` command calls the latter, so a
test exercises the exact path without consuming the week's guard.

**Weather-aware planning (EP-13)**: a daily job (`bot.jobs.weather_plan_job`, `run_daily` at
`WEATHER_PLAN_HOUR` Europe/Warsaw, before the morning window) that proposes moving/easing a
**key session** (tempo/intervals/long — `ADAPT_HEAVY_TYPES`) that lands on an
extreme-weather day. `weather.fetch_forecast_week` extends the Open-Meteo lookup to 7 daily
rows (same compact shape as the today dict, no hourly); `weather.find_weather_conflicts` is a
**pure, network-free** filter (heat `feels_max_c ≥ WEATHER_HEAT_FEELS_C`, rain
`precip_prob_pct ≥ WEATHER_RAIN_PROB_PCT`, wind `wind_max_kmh ≥ WEATHER_WIND_KMH`, or icy
WMO code / freezing max-temp) over sessions within the next `WEATHER_DECISION_DAYS` — **no
conflict ⇒ zero Claude calls, total silence** (the AC). On a conflict, `run_weather_plan_check`
(`SYSTEM_WEATHER_PLAN`, Sonnet `MODEL_PLAN`, `kind="weather"`) returns a `PlanEdit` filtered to
**move/modify only, within `today..today+decision_days`** (`_filter_weather_ops` — never
skip/add: weather doesn't cancel training, only reschedules), and the summary always says
"прогноз на зараз" (no auto-apply — the forecast may shift). The proposal reuses the EP-02
machinery: `_send_adapt_proposal` → `PENDING_ADAPT_KEY` → `adapt_callback` (`apply_plan_ops` +
`resync_workouts`). Gated on a stored location + active plan + `plan_adapt_enabled` (the
general auto-adjust switch; no location ⇒ feature just doesn't activate). The **"don't ping
twice" pitfall** is enforced by `_has_pending_proposal`: all three automatic proposers (weekly
adapt, morning nudge, weather) skip when an unanswered proposal is already pending — a single
`✅/❌` at a time, never overwriting the last one's stored ops. Not dedup-cached (like adapt —
`_complete` has no cache). No test/force command yet (chat-only concern is the plan itself).

**Personal records (EP-14)**: a **pure-Python, zero-LLM** detector (`app/records.py`) over data
already in the DB — no network, no Claude, cheap enough to run on every morning tick. Categories:
fastest ~5K/~10K/~half (min avg pace among whole runs within ±5% of the distance; pace floored at
2:30/km to reject GPS junk), longest distance + longest duration, biggest ISO-week km, all-time
VO2max, and best race prediction per distance (5K/10K/half/marathon, from `daily_metrics.extra`).
`PersonalRecord` (`kind`/`value`/`previous_value`/`activity_id?`/`date`) keeps the **history** of
records, not just the current best — each beat inserts a new row carrying the value it dethroned.
`records.detect_records` recomputes every category and inserts only genuine improvements (idempotent;
pace/longest lower-or-higher-better per `_HIGHER_BETTER`; race predictions need a ≥10s margin to
beat the daily jitter). The **backfill-vs-fresh** distinction (AC: no celebrations during import) is
a **date gate**, not a flag: every record carries the real date it was achieved (the activity date,
the last-run date of a record week, or the daily-fetch date for VO2max/race), so a first run over
years of history dates its bests in the past and `announce_worthy` (within `FRESH_DAYS`) filters them
all out — only a record set in the last few days earns a 🎉. Wired in three places: the morning tick
(`_records_check_for_user` in `bot/jobs.py`, right after the activity auto-analysis — commits then
DMs `records.celebrate`, so a Telegram failure never re-opens an already-recorded PB), the `/records`
command (current bests, empty-state message, honest "whole activities only" caveat — v1 doesn't scan
`series` for a fast 5K *inside* a longer run), and the report/digest context: `run_analysis` +
`run_digest` feed recent records to Claude (**and into the dedup-cache key** — the README pitfall) so
a fresh PB gets a line in the morning report / weekly digest. CLI `backfill-records --email` seeds the
table silently from full history (run once after `import-export`).

**Personal baselines (NF-01)**: a **pure-Python, zero-LLM** "today vs your norm" over the daily
history already in the DB (`app/baselines.py`). A number like "RHR 52" only means something
against *your own* history, not a generic scale, so `compute_baselines` turns
`repository.read_history(days=WINDOW_DAYS)` (90 days, oldest-first) into rolling percentiles
(p25/p50/p75) per recovery metric — `resting_hr`, `hrv_avg`, `sleep_score`, `sleep_h`,
`stress_avg`, `bb_charged` — emitting a compact `norm` snapshot `{window_days, metrics:{<k>:{cur,
p50, band:[p25,p75], n, pos}}}`. `cur` is the most-recent non-null value (today, or the last
synced day); `pos` is a **neutral** low/normal/high vs the band (the SYSTEM prompt carries the
per-metric **valence** — low RHR/stress is good, low HRV/sleep is bad). A metric with fewer than
`MIN_SAMPLES` (14) days is skipped; no metric qualifying → `norm=None` (new user works without it).
Percentiles are numpy-free and robust to the gaps a backfill leaves. Wired into `run_analysis`
(report/morning, **not** `/deep`, in the same `user_id`-gated block as `fitness`/`records`) →
`analyze_with_stats` (`user_content["norm"]`) **and into `_cache_key`** (the README naskrізна
pitfall: all Claude context must key the dedup cache). The LLM computes nothing — it only narrates
the ready deviations (SYSTEM section «ТВОЯ НОРМА»). Scope is a single 90-day window; the ticket's
30/365 + seasonal windows are a documented future extension. `tests/test_baselines.py`.

**Injury-risk radar (NF-04)**: a **pure-Python, zero-LLM** detector (`app/injury.py`) that fuses
four early-warning signals already in the DB into one severity score — the load-side detector the
backlog imagined next to EP-08. Signals: **repeated pain** (same body part flagged ≥2× in 14 days,
from EP-12 `subjective` — weighs heaviest, the strongest predictor), **sustained high ACWR**
(`extra.acwr_pct` ≥140 on ≥3 recent days), **RPE rising at a stable pace** (harder for the same
speed → early fatigue/illness), and **recovery drift** (HRV below its baseline band several days +
resting-HR drift up). `injury.assess(daily, runs, history_days=...)` → an `Assessment`
(`level` calibrating/none/elevated/high, `score`, `signals`). **Calibration gate** (the EP-08
false-positive rule): `level="calibrating"` and no warning until `INJURY_MIN_HISTORY_DAYS` (14) of
history. Repository readers `read_load_history` / `recent_subjective_runs` / `count_daily_metrics`
feed `service.build_injury_assessment` (pure, zero-LLM — used by both `/risk` and the job).
`service.run_injury_check` narrates an actionable assessment via Sonnet (`SYSTEM_INJURY` — cautious,
non-medical) but **falls back to the deterministic `injury.summary`** if the LLM fails (the warning
never depends on the LLM); logs `ReportLog(kind="injury")`. Surfaced two ways: the **`/risk`** bot
command (instant DB read — shows calibrating/clear/signals) and a **morning-tick hook**
(`_injury_check_for_user` in `bot/jobs.py`, after the records check) that DMs one advisory when
actionable, guarded to at most once per `INJURY_GUARD_DAYS` (5) via `bot_state` `injury_warned`
(guard set before the send so a hiccup can't loop). Process-level `INJURY_RADAR` on/off (personal
app; no per-user column). Deeper EP-02 auto-deload integration is left as a future step (the ticket
sits "on top of EP-08+12"). `tests/test_injury.py`.

**Proactive health alerts (EP-08)**: a **pure-Python, zero-LLM** recovery-anomaly detector
(`app/health.py`) — the *recovery/illness* sibling of the injury radar (same "risk signal" chassis,
different rules: NF-04 fuses **load** signals into injury risk, EP-08 watches **recovery** metrics
drifting the wrong way). The key idea (ticket: "базлайни NF-01 дають кращі пороги, ніж хардкод"):
reuse NF-01's **personal percentile bands** (`baselines.compute_baselines` over ~90 days) as the
thresholds, and flag a metric that has sat **outside your band** in the unhealthy direction for
**several recent days** — `hrv_low` (HRV below your p25 ≥3 of 7 days), `rhr_up` (resting HR above
p75 ≥3 days), `sleep_debt` (sleep below band ≥4 of 7 days), `stress_high` (stress above p75 ≥3
days). Each → a typed `Alert(kind, severity, detail, advice)`; `health.detect(history)` →
`HealthReport(level calibrating/none/alert, alerts)`. **False-positive guards** (the EP-08 pitfall):
personal thresholds (not generic cutoffs), sustained-not-a-blip, a **cold-start gate**
(`HEALTH_MIN_HISTORY_DAYS`=7 → quiet `calibrating`; a metric also needs NF-01's 14 samples before
its band exists, so early history is naturally silent), and **non-medical** advice
(`health.summary` — the deterministic LLM-free fallback). `service.build_health_alerts` (pure,
zero-LLM, shared by `/health` and the job) reuses the same 90-day slice; `service.run_health_alert`
narrates an actionable report via Sonnet (`SYSTEM_HEALTH`, cautious/non-diagnostic) with the
deterministic fallback; logs `ReportLog(kind="health")`. Surfaced two ways: the **`/health`** bot
command (instant DB read — calibrating/clear/alerts) and a **morning-tick hook**
(`_health_check_for_user` in `bot/jobs.py`, after the injury check) that DMs one advisory for any
**newly** actionable alert kind, guarded **per-rule** via `bot_state` `alert:<kind>` (cooldown
`HEALTH_ALERT_COOLDOWN_DAYS`=3, so a persistent drift isn't re-flagged daily but a fresh anomaly
still fires; guard set before the send). To avoid stacking two risk pings, the tick **skips the
health push when an injury advisory already went out today** (at most one risk DM/day). Process-level
`HEALTH_ALERTS` on/off + a **per-user** `User.alerts_enabled` toggle (settings form; default on;
deactivated/disabled → silence). Not fed into the daily report context — the report already gets
recovery context from NF-01 `norm`; that synergy is a documented future extension.
`tests/test_health.py`.

**Multisport weekly load budget (NF-05)**: a **pure-Python, zero-LLM** cross-sport training-load
budget (`app/multisport.py`). Our `weekly_run_volume` only sees runs, so a 3h kite session or an
evening of tennis before intervals is invisible — the plan stacks a hard run on hidden fatigue.
`multisport.weekly_load(activities)` turns **all** activity types into a TRIMP-like load per ISO
week, broken down by sport (`run`/`bike`/`swim`/`strength`/`other`) with a `non_run_pct` share.
Design choice: **one uniform load metric** (HR-based Edwards zone weight `dur_min × 1–5`, with a
per-sport duration fallback when HR is missing/unreliable — kite/tennis under a wetsuit/racket arm)
rather than Garmin's per-activity `load`, which is only populated for some sports and would
systematically inflate runs — defeating a fair run-vs-not comparison. `repository.weekly_activity_load`
fetches the rows (any type); `service._build_multisport` shapes `{weeks, this_week}` (this-week vs
last headline via `budget_summary`) or `None` when there's no load. Fed into **plan generation**,
**plan adaptation** (EP-02) and the **weekly digest** (EP-07) contexts — and into the digest
`_digest_cache_key` (the naskrізна pitfall). Prompts (`SYSTEM_PLAN`/`SYSTEM_PLAN_ADAPT`/
`SYSTEM_DIGEST`) read `multisport` to avoid a hard run next to heavy cross-training and to temper
run volume when `non_run_pct` is high. Not in the daily report (matches the ticket's adaptation +
digest scope). The seasonal-accent intake (kite-season ⇒ less run volume) is a documented future
extension. `tests/test_multisport.py`.

**Compare-past-self (NF-06)**: "am I fitter than a year ago?" — the deep GDPR-backfilled history
made visible. `app/compare.py` is the **pure-Python** part: `window_pair(today, weeks, years_back)`
picks the current `weeks`-long window and the **same calendar span** N years ago (Feb-29-safe),
`parse_period` reads the `/compare [weeks]` arg (leading digits; default 4), `has_signal` bails when
there isn't enough in **both** windows, `fmt_range` renders the header. `repository.window_stats`
aggregates each window (one query pair): run km/count/longest, **median** typical pace, avg run HR,
avg HRV/sleep/RHR, best VO2max, best race predictions. `service.run_compare` assembles both windows,
narrates via **one Sonnet call** (`SYSTEM_COMPARE`, `MODEL_COMPARE`), dedup-caches on the two
windows + framing (`_compare_cache_key`), logs `ReportLog(kind="compare")`, and returns `None` when
`has_signal` fails (caller shows a friendly "not enough history" message). The prompt's core job is
**honesty**: it must flag different seasons/conditions and thinner data rather than over-claim.
Surfaced two ways: the **`/compare [тижнів]`** bot command (pure DB read + decrypt creds directly —
no Garmin fetch, no MFA risk) and a **monthly auto-block** riding on the first weekly digest of each
calendar month (`_monthly_compare_for_user` in `bot/jobs.py`, guarded via `bot_state`
`compare:<YYYY-MM>` — the guard is set only after a message goes out, so a no-history month retries
next week). `tests/test_compare.py`.

**Models**: `/report` + morning + `/ask` + `/activity` + weekly digest use `claude-sonnet-5`; `/deep`
and **training-plan generation** (`MODEL_PLAN_GEN` — reasoning-heavy + infrequent, so the
cost is fine) use `claude-opus-4-8`. Plan **edits** (`/plan <text>` → ops) stay on Sonnet
(`MODEL_PLAN`) — small and mechanical. Plan generation also accepts a **Fable** engine via
the setup-form toggle (see the strength/plan section). Every call is logged to `ReportLog`
(tokens, cost, ok/error). `PRICES` (Anthropic list prices, $/1M in/out): Sonnet 5 **intro**
$2/$10 through 2026-08-31 (bump to $3/$15 on 2026-09-01), Sonnet 4.6 $3/$15, Opus 4.8
$5/$25, Fable 5 $10/$50. NB Sonnet 5 uses the newer tokenizer (~30% more tokens for the
same text than Sonnet 4.6), so per-request token counts rise.

**`/ask <question>`**: cheap follow-up Q&A (Sonnet) grounded in the last `ASK_DEFAULT_N`
(3) **daily** reports' text — no Garmin fetch, no payload. `run_ask` reads
`repository.get_recent_reports` (filtered to `kind="report"`, so `/deep` and prior
`/ask` answers don't pollute the daily context), **plus** `get_recent_asks` — this
user's `/ask` exchanges (question + answer) from the last `ASK_CONTEXT_MIN` (5) minutes,
so a follow-up can build on the previous one. Both go to `analyze_with_stats`' sibling
`ask_with_stats` (separate `SYSTEM_ASK` prompt; the recent thread arrives as `recent_qa`
and is part of the dedup-cache key), which logs a `ReportLog` row with `kind="ask"`.
Bot-only — no web endpoint.

**Stored question**: `ReportLog.question` records the asked prompt — for `/ask` (the
question), `/deep` (the user's question) and morning (its fixed prompt); `/report` leaves
it null (default daily prompt). Visible in the `/me` and `/ui` browsers, and what
`get_recent_asks` reads back for the conversation thread.

**Day-over-day continuity**: `run_analysis` (report/morning, not `/deep`) feeds the
**previous day's** daily report as `previous_report` context via
`repository.get_last_report` — which excludes today's reports and `/deep`+`/ask`. Excluding
today keeps the dedup-cache key stable across repeated same-day `/report` presses (so a
second press is a `CLAUDE CACHE HIT`, not a paid re-run).

**Exercise names**: `fetch_exercise_summary` reads Garmin's specific `name` code, maps it
to Ukrainian via `app/garmin/exercise_names.py` at return time (cache stays language-
neutral). Unknown codes are logged once (`EXERCISE unmapped: <CODE>`). Warm-up jog filtered.

**Run pace/HR series**: for running activities, `fetch_activity_series` pulls Garmin's
`/details` metrics, resolves the speed/HR/distance columns by descriptor key (indices
vary), converts m/s→min/km, downsamples to ~150 points, and stores them on
`ActivityRecord.series`. The `/ui` and `/me` browsers show a **minimal column set** on the
activities list (`admin.INDEX_COLS`) and render `series` as **pace + HR sparklines** on the
per-row detail page (`admin._run_series`/`_run_charts`) — the detail routes stay pure DB
reads (no Garmin call). Non-run activities have `series = null`. The detail charts also
carry per-point data (`s.pts`: x-fraction + raw value + distance) and a small inline
vanilla-JS hover handler in `detail.html` that shows the value (pace as m:ss, HR in уд)
and distance on mousemove — progressive enhancement; the SVG still renders without JS.

**`/activity` analysis**: `/activities` lists this user's last 5 activities (DB read, no
Garmin call) keyed by the short DB `id`; `/activity <id>` analyzes one. `run_activity_analysis`
builds a compact payload (`activity_payload`: summary + `_segments` — the run's series
collapsed to ~6 pace/HR segments so the LLM sees pacing and HR drift), calls Sonnet with
`SYSTEM_ACTIVITY`, **stores the text on `ActivityRecord.analysis`** (shown as a block on the
web detail page) and logs a `ReportLog` (kind="activity"). Shares the dedup cache
(`_activity_cache_key`). Works for any type; runs additionally get the segment detail.

**Post-run check-in (EP-12)**: after the auto-analysis DM (and on `/activity`) the bot
attaches an inline keyboard — RPE 1–10 (one tap) + «🩹 щось боліло» → body-part buttons
(коліно/гомілка/…). It's **stateless**: the DB activity id is baked into the callback data
(`ci:rpe:<aid>:<n>` / `ci:pain:<aid>` / `ci:part:<aid>:<slug>` / `ci:ok:<aid>`), so no
`context.user_data` is needed (the ticket's pitfall — niggles are buttons, not free text).
`repository.set_subjective` merges `{rpe, pain?, note?}` into `ActivityRecord.subjective`
(re-tap overwrites; «без болю» clears any note); the buttons quietly `edit_message` away
after the answer. `/checkin [rpe] [note]` is the manual fallback for the last activity.
Silence is valid — nothing nags, analysis works without it. **Consumer phase 1**:
`activity_payload` includes `subjective`, so `SYSTEM_ACTIVITY` sees felt effort/pain (and
it enters `_activity_cache_key` automatically — the README pitfall). `/me` shows RPE + pain
on the activity card and detail. **Consumer phases 2–3** (`app/subjective.py` — a pure,
zero-LLM aggregator over `repository.recent_subjective_runs`, sibling to `injury.py` but
just *shaping* the felt-effort signal rather than scoring it): `subjective.summarize(runs)`
→ a compact `{n, avg_rpe, rpe_rising, recurring_pain?, recent}` snapshot fed to three
narration prompts — (a) the **daily/morning report** (`run_analysis` → `analyze_with_stats`
`user_content["subjective"]` **and `_cache_key`** — the наскрізна pitfall; `SYSTEM` section
«САМОПОЧУТТЯ / ЧЕК-ІНИ» flags a recurring niggle / rising effort at the same pace), (b) the
**EP-02 plan adaptation** (`run_plan_adaptation` context + `SYSTEM_PLAN_ADAPT` rule — ease
when effort trends up / pain recurs even when objective load looks fine; not cached), and
(c) the **weekly digest** via a plan/fact **`overreached`** count in
`repository.weekly_compliance` (an *easy-intent* session — easy/recovery/base/long — done but
whose check-in RPE was ≥`subjective.HARD_RPE`=8: "did it, but it felt much harder than the
session called for"; rides inside `compliance`, already in `_digest_cache_key`;
`SYSTEM_DIGEST` reads it as an under-recovery signal). `rpe_rising` reuses the same
pace-stable/RPE-up test as `injury._rpe_signal`; `recurring_pain` the same ≥2×/14d rule.
`tests/test_subjective.py`.

**Training plans**: a user picks a goal + intake on the **web form** (`/plan`); we *prescribe*
a dated program (distinct from the Garmin-Calendar `planned_runs` we merely read). This is
the one place we need **structured LLM output**: `SYSTEM_PLAN` returns JSON validated by
`GeneratedPlan`/`PlanWorkout` (`_coerce_plan` slices to the outer `{...}`; one retry, else
`AnalystError`). Each workout carries both a human `description` (warmup/cooldown, pace as a
**range**, HR/fuel cues) and structured `steps` (`PlanStep` — recursive: warmup/run/recovery/
cooldown/repeat, `dist_m`|`dur_s`, `pace_min_km` `[fast, slow]`, mirrors the Runna
`planned_runs[].detail.steps` shape) — persisted on `PlannedWorkout.steps` and rendered as
chips on `/plan` (`plan._fmt_step`); the `steps` are also what a future Garmin-Connect workout
export maps from. Generation runs on **Opus** (`MODEL_PLAN_GEN`, `max_tokens=16000` so a long
plan with steps fits). `run_plan_generation` feeds compact context (recent runs + recovery
trend, weekly volume, fitness/load snapshot),
persists a `TrainingPlan` + `PlannedWorkout` rows via `repository.create_plan` (archiving any
prior active plan), and logs `ReportLog(kind="plan")`. Adjustments are **free-text in the
bot**: `/plan <текст>` → `run_plan_edit` (`SYSTEM_PLAN_EDIT` → `PlanEdit` operations
add/move/modify/skip) returns a *proposed* change; the bot shows it with inline ✅/❌ buttons
(`plan_callback`, pending ops in `context.user_data["pending_plan"]`) and only
`repository.apply_plan_ops` on confirm. **Risky edits** (a big distance/intensity jump, etc.):
`PlanEdit.operations` always holds the *literal* request, but the prompt also sets `risky` and
returns a safer counter-proposal (`alt_summary`/`alt_operations`); the bot then offers a third
button (✅ as-asked / 🛡 take-suggestion / ❌ cancel — `plan_apply` / `plan_apply_alt` /
`plan_cancel`), so the user decides with the risk spelled out. Plain `/plan` shows upcoming
workouts. The shared `_complete` helper centralises
the Claude call for both. **Recovery-adaptive behaviour** (EP-02): `run_plan_adaptation`
(`SYSTEM_PLAN_ADAPT` → the same `PlanEdit` ops + confirm buttons) runs from two hooks in
`bot/jobs.py` — a weekly review (`plan_adapt_job`, Sunday `PLAN_ADAPT_HOUR`) over the next
14 days, and a morning nudge (`_adapt_morning_check`, inside the morning tick) that fires only
when today holds a heavy session (tempo/intervals/long) and readiness is below
`PLAN_ADAPT_READINESS_MIN`. Ops outside `today..today+window_days` are dropped
(`_filter_ops_to_window`); `User.plan_adapt_enabled` is the global master switch. **Adjust
level** (ST-07, per-plan `intake["adjust_level"]`: off / conservative / flexible; picked on
the setup form, changeable on `/plan` via `POST /plan/adjust-level` without regeneration;
unset → `plan_adjust_level` defaults by goal: `target_date` ⇒ conservative, else flexible):
bounds *how bold* adaptation may be — `off` skips the Claude call entirely
(`run_plan_adaptation` → `(plan, None)`); `conservative` allows only modify (volume cut ≤30%)
and move ≤2 days — enforced by `_filter_ops_to_level`, not just the prompt — with a stricter
taper mode ≤14 days to `target_date` (no moves, cut ≤15%); `flexible` allows the full
spectrum incl. skip/token-2km. Adapt calls are NOT dedup-cached (`_complete` has no cache),
so level/context changes always take effect. NB the prompt-for-JSON + Pydantic + one-retry
choice avoids SDK tool-use, matching the rest of the `messages.create` usage.
**Open-ended "keep improving" plans**: a fifth goal `general` (`GOALS`/`OPEN_ENDED_GOALS`
in `routers/plan.py`, `plans.OPEN_ENDED_GOAL`) with **no target race** — a rolling plan you
just keep running. Generation lays a first block of `PLAN_BLOCK_WEEKS` (6) weeks: the plan
is stored with `target_date=None` (so all the `target_date`-guarded logic treats it as
open — adjust-level defaults flexible, no taper), while the model gets a concrete block-end
as its range plus an `open_ended` flag (`SYSTEM_PLAN`: no подводка, sustainable progression
with room to grow). A daily job (`bot.jobs.plan_extend_job` → `_extend_for_user`, `run_daily`
at `PLAN_EXTEND_HOUR`=4, before plan-sync) **auto-extends** it: when the plan's last workout
is within `PLAN_EXTEND_LEAD_DAYS` (10), `analysis.plans.run_plan_extension` **appends** the
next 6-week block to the SAME plan (`repository.append_workouts` — never archives/regenerates)
continuing progression from the tail (`previous_weeks` context) and rebasing `week` numbers
(`week_offset` = current max week). The gate is self-limiting (after a top-up the last date
jumps ~a block out, so it won't re-fire; a failed gen just retries next day) — no bot_state
guard. Strength is extended too, reusing the first block's custom sessions + re-cloning saved
templates (`_add_plan_strength(..., reuse_only=True)`, windowed via `add_strength_workouts`'s
`start`/`end`/`week_offset`) — **no extra Claude call**. Best-effort Garmin re-sync after; the
user gets a short "додав наступні тижні" DM. Extension is auto-only (per the setup choice) —
no manual button yet. **Cost note**: this is the one path that fires a real Opus call on a
schedule (the user's own key), so it's gated tight and silent for everyone with runway left.

## Caching layers

- **Claude dedup** (`llm_cache` table, PERF-02): keys on a hash of the meaningful
  payload (`daily`, `recent_activities`, `planned_runs`) + date + question + model +
  `previous_report`. The volatile `generated` timestamp is deliberately excluded —
  otherwise the key changes every minute and never hits (the main gotcha if you touch the
  key logic). `/ask` keys instead on the recent reports + `recent_qa` thread + question +
  model. 1-week TTL; expired rows purged lazily on write. Hit logs `CLAUDE CACHE HIT`.
  The get/put lives in the async `run_*` wrappers (`app.db.llm_cache` — they hold the
  session; the sync `*_with_stats` functions run in a threadpool and stay cache-free),
  so the bot and web processes share hits — the old per-process `claude_cache.json`
  paid twice for the same call and its whole-file rewrites lost the other process's
  entries. Cache failures are best-effort: a failed read is a miss, never an error.
- **Garmin disk cache** (per-key files in `GARMIN_CACHE_DIR`, PERF-02): immutable
  ID-keyed assets only — `exercise:v2:<id>` (365d), `workout:v2:<id>` (7d; name + coach
  description + steps), and `series:v1:<id>` (365d; a run's per-point pace/HR from
  `/details`, downsampled). One JSON file per key (atomic replace, cross-process safe)
  fronted by an in-process memo; the legacy single `garmin_cache.json` is split into
  per-key files once at import, then renamed `.migrated`. Day-level caching moved to
  the DB.
- **DB day-level cache** (`DailyMetric`): past days served from the DB; today refetched.

## Concurrency & rate limiting (PERF-04b / PERF-05)

- **Dedicated Claude thread pool** (PERF-04b): every `*_with_stats` Claude call runs on
  a small `ThreadPoolExecutor` (`CLAUDE_MAX_WORKERS`, `thread_name_prefix="claude"`) via
  `analysis.service._run_claude`, **not** the shared anyio threadpool that Garmin
  logins/fetches use — so a burst of multi-second LLM calls can't starve the pool fast
  Garmin/DB work needs. The sync functions keep their signatures (tests monkeypatch them;
  retry/`AnalystError`/`ReportLog` behaviour unchanged) — the executor was chosen over
  `AsyncAnthropic` for exactly that minimal blast radius. The `_get_client` per-key client
  cache is unchanged. Garmin fetches inside the `run_*` wrappers (`client.fetch_workouts`,
  `client.fetch_workout_full`) deliberately stay on `run_in_threadpool`.
- **Grouped day-fetch** (PERF-04b): `build_payload_cached` fetches all missing past days
  **plus today** in ONE `run_in_threadpool` hop (`service._fetch_days` loops inside) instead
  of a round trip per day.
- **Garmin rate limiter + 429 backoff** (PERF-05): a process-wide `client._RateLimiter`
  (leaky-bucket spacer — reserve the next slot under a `threading.Lock`, sleep outside it;
  synchronous because the client runs in the threadpool) throttles **every** connectapi
  call (`client._api`) to `GARMIN_RPS`. Post-Cloudflare a polite, predictable request
  pattern is survival, not tuning. A 429 (`_is_rate_limited` — nested `.error.response`
  status or string fallback) is retried `GARMIN_RETRIES` times with exponential backoff;
  after exhaustion the exception propagates so callers keep prior behaviour (`_safe` logs +
  returns `{"_error": ...}`; write calls surface it). The MFA login gate (a ~25s human
  wait in `app.garmin.mfa`) is a **separate path** — never throttled or retried.
- **Per-user fetch lock** (PERF-05): `build_payload_cached` wraps its fetch+persist phase
  in a per-user `asyncio.Lock` (`service._user_fetch_locks`, a `WeakValueDictionary` so idle
  users' locks are GC'd). A morning tick and a concurrent `/report` for the same user no
  longer both hammer Garmin for the same days. A 30s memo (`_recent_payload`, keyed by
  `(user_id, days, activity_limit)` so a narrow tick never serves a wider `/deep` request)
  lets the second (blocked) caller **reuse** the just-built payload instead of re-fetching
  today (~a dozen calls); the reuser gets `new_activities=[]` so auto-analysis never
  double-fires. Different users take different locks — no cross-user blocking.

## Logging

`app.core.logging.setup()` runs at process start (web factory and `bot.main`). Logs go to
`bot.log` (rotating, 5 × 1 MB) and stdout. Root level is `LOG_LEVEL`; noisy libraries
(httpx, telegram, apscheduler, uvicorn.access) are pinned WARNING. Run with
`LOG_LEVEL=DEBUG` to see skip-reason lines (e.g. `MORNING skip: outside window`).

Web requests are logged by an app-level HTTP middleware in `create_app` (logger `api`,
`GET /plan → 200 42ms`; `/health` skipped) instead of `uvicorn.access`, so they share the
project format. Per-Claude-call cost/tokens are logged (logger `claude`) **and** persisted
to `report_logs` (browsable at `/me/report_logs` and `/ui/report_logs`).

## TODO

- Validate the `gconn` provider against the live Garmin API.
- Deploy to Raspberry Pi 4 (systemd units for `bot.main` and `uvicorn`).
- Optional: dashboard/history visualization.
