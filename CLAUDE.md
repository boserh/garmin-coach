# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## ⚠️ Git commit identity — HARD RULE (do not skip)

**Never commit as `Claude <noreply@anthropic.com>`.** Every commit (author *and*
committer) must carry the repo owner's identity: `Serhii Bodnaruk <sergiwez@gmail.com>`
(match whichever of `sergiwez@gmail.com` / `32734554+boserh@users.noreply.github.com`
recent commits on the target branch use). Do **not** change global/local git config to
do this (`user.name`/`user.email` stay untouched) — instead pass `--author=` on the
commit and set `GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL` for that one command, e.g.:

```bash
GIT_COMMITTER_NAME="Serhii Bodnaruk" GIT_COMMITTER_EMAIL="sergiwez@gmail.com" \
  git commit --author="Serhii Bodnaruk <sergiwez@gmail.com>" -m "..."
```

**This applies to EVERY command that creates or rewrites a commit** — not just
`git commit`: `git cherry-pick`, `git rebase`, `git commit --amend`, `git revert`, `git
merge` all silently set the *committer* (not the author) to whatever the ambient git
identity is, which in this environment defaults to `Claude <noreply@anthropic.com>`.
`git cherry-pick` in particular preserves the original *author* from the source commit,
so it's easy to check `%an` and see the right name while `%cn` is still wrong. Prefix
**every one of these commands** with the same `GIT_COMMITTER_NAME`/`GIT_COMMITTER_EMAIL`
env vars, e.g. `GIT_COMMITTER_NAME="Serhii Bodnaruk" GIT_COMMITTER_EMAIL="sergiwez@gmail.com" git cherry-pick <sha>`.

After ANY rebase/cherry-pick/amend/merge, verify the **whole range** you touched, not
just the tip — `git log --format='%h %an <%ae> / %cn <%ce>' <base>..HEAD` — before
pushing or opening a PR. Fix (interactive rebase, or reset+recommit with the env vars)
before considering the task done.

**Also strip any `Co-Authored-By: Claude ...` / `Claude-Session: ...` trailer** some
tooling appends to the message body by default — those are wrong too, not "unrelated": no
Claude attribution anywhere in the commit, body included.

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
| `GARMIN_RPS` | `3.0` | Process-wide Garmin request rate cap (req/s); `0` disables the limiter (PERF-05) |
| `GARMIN_RETRIES` | `2` | 429 retries with exponential backoff inside `client._api` (PERF-05) |
| `CLAUDE_MAX_WORKERS` | `4` | Size of the dedicated Claude thread pool, off the shared anyio pool (PERF-04b) |
| `DATABASE_URL` | `sqlite+aiosqlite:///./garmin.db` | DB; switch to `postgresql+asyncpg://...` by env alone |
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
| `FUELING_MIN_DURATION_MIN` | `45` | NF-11: below this estimated session duration, the fueling advisor stays silent |
| `FUELING_HEAT_FEELS_C` | `28` | NF-11: feels-like max °C at/above → heat notes (electrolytes, coolest hourly slot) |
| `DEPLOY_ENABLED` | `False` | OPS-03: master on/off for the admin-only `/deploy` bot command (git pull + service restart) |
| `SLEEP_NUDGE` | `True` | NF-16: master on/off for the evening sleep-debt nudge |
| `SLEEP_NUDGE_HOUR` | `21` | NF-16: hour (Europe/Warsaw — the job's own schedule stays process-TZ in v1) the evening check runs |
| `GEAR_WEAR_KM` | `700` | NF-15: mileage threshold that triggers a "replace your shoes" DM once per pair; `0` disables the DM (roster/mileage still refresh) |
| `GEAR_REWARN_KM` | `150` | NF-15: re-warn a pair still in rotation every this many further km past the first warning |

`CLAUDE_CACHE_FILE` is gone — the Claude dedup cache lives in the `llm_cache` table
(PERF-02), shared by the bot and web processes.

`STATE_FILE` is gone — the morning-sent date lives in the `bot_state` table, per user.

## Authentication & multi-user

- **Users**: `users` table (login email + bcrypt hash, `is_admin`, encrypted
  Garmin/Claude creds + garth token, plaintext indexed `telegram_chat_id`, a
  `weather_location`/`latitude`/`longitude` for the morning weather lookup, a `timezone`
  (IANA string, default `Europe/Warsaw` — ST-14, drives per-user job windows/guard dates),
  and the per-user feature toggles `garmin_sync_enabled`/`plan_adapt_enabled`/`alerts_enabled`
  — the last governs EP-08 health alerts and NF-16's sleep nudge). Web login is a signed
  cookie session (`SessionMiddleware`, signed by `APP_SECRET_KEY`).
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
  `import-garth-token --email [--path ~/.garth]` seeds a user's garth session from a
  token dir (`--path` defaults to `~/.garth`);
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
  `trigger-plan-adapt --email` runs the weekly plan-adaptation review (EP-02, same call
  as `plan_adapt_job`) on demand from the console instead of waiting for Sunday — a real
  Claude call, and when it proposes a change it sends the normal ✅/❌ proposal to the
  user's Telegram chat via a standalone `telegram.Bot` (reusing `bot.jobs._send_adapt_proposal`
  so the confirm/reject flow is unchanged) — console-triggered, Telegram-delivered.
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
  charts.py            inline-SVG chart helpers (series/trend_series/run_series/run_charts) — shared by admin/me/dashboard (EP-04)
  mcp_server.py        NF-08: personal read-only MCP server (stdio) over the same /ask tools
  deploy.py            OPS-03: git pull + systemd restart subprocess wrappers, bot-triggered
  race.py              EP-05: race-pack target/distance mapping + narration-context builder
  gear.py              NF-15: shoe-mileage parsing (defensive) + wear-threshold/rewarn logic
  analysis/
    service.py         analyze/ask/run_analysis/run_ask; per-key Anthropic client; dedup cache
    prompts.py         SYSTEM + SYSTEM_ASK_TOOLS prompts
  routers/
    auth.py            GET/POST /login, GET /logout
    settings.py        /settings (own creds), /admin/users (admin)
    dashboard.py        GET /dashboard — mobile-first overview, login, per-user (EP-04)
    health.py          GET /health (public), GET /status (login, per-user)
    reports.py         GET /report.json (Sonnet), GET /deep (Opus) — login, per-user
    history.py         GET /history?days=N — trends from DB, login, per-user
    plan.py            GET/POST /plan — training-plan setup form + view, login, per-user
    chat.py            GET/POST /chat, POST /chat/confirm — web chat over run_ask/run_plan_edit (EP-11)
    admin.py           /ui DB browser — admin only
  dependencies.py      shared deps (get_session)
bot/
  main.py              builds the Application, registers handlers + job, run_polling
  handlers.py          /report, /ask, /deep, /activities, /activity, /records, /costs, /gear, /compare, /wrapped, /insights, /risk, /health, /goal, /race, /plan (+edit), /sick, /deploy (OPS-03, admin), /test_*; _resolve_user, error handler
  jobs.py              morning_job loops users (per-user timezone window, ST-14; per-user once-a-day guard); also weather_plan_job/plan_adapt_job/weekly_digest_job/sleep_nudge_job
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
- `GET /dashboard` — mobile-first overview (EP-04): readiness today, 30-day HRV/RHR/
  sleep/stress trends, next 7 days of the active plan, last 5 activities, this month's
  AI cost. Pure DB read (no Garmin/Claude). Login; current user; the post-login/root
  redirect for a non-admin.
- `GET /status` — the logged-in user's Garmin auth, DB stats, last morning report, cost.
- `GET /report.json` — daily report (Sonnet). Login; current user.
- `GET /deep?q=...` — deep analysis (Opus). Login; current user.
- `GET /history?days=N` — HRV/sleep/stress/body-battery trend from the DB. Login; current user.
- `GET/POST /plan` — training-plan setup form (no active plan) / plan view; `POST /plan/archive`
  (archive active), `POST /plan/adjust-level` (ST-07), `POST /plan/season` (EP-05 NF-12:
  seasonal accent, without regeneration), `GET /plan/archive` (list archived), `GET /plan/{id}`
  (read-only view of a past plan). Login; current user.
- `GET/POST /chat` + `POST /chat/confirm` — EP-11: web chat over the same `run_ask`/
  `run_plan_edit` engines as the bot; a plan-edit proposal's ✅/🛡/❌ confirm state lives in
  `bot_state`, shared with the Telegram flow. No streaming yet (deliberate v1 scope — see
  CLAUDE.md's EP-11 section). Login; current user.
- `GET /me/export` — NF-13: a streamed ZIP of everything this account owns (daily_metrics/
  activities JSON+CSV, personal_records/plans/report_logs JSON) — pure DB read scoped to
  `user.id`; the `users` row is never touched, so credentials/tokens can't leak by
  construction. Not a substitute for OPS-02's DB backup (portability, not disaster recovery).
  Login; current user; linked from `/me`.
- `GET /settings` — manage own Garmin/Claude/Telegram creds (encrypted on save).
- `GET /admin/users` — list/create users (admin only).
- `GET /ui` + `GET /ui/{table}` + `/ui/{table}/{id}` — raw DB browser (whitelisted
  tables: users, daily_metrics, activities, report_logs, bot_state). **Admin only.**
  Templates in `app/templates/`.

Auth: a signed cookie session set at `/login` (no token headers). `current_user`
gates user endpoints; `require_admin` gates `/ui` and `/admin/users`.

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

**Token-expiry warning (ST-11)**: OPS-01's `token_info.decode_token_info` could always tell you
a token's ~1y death date, but nobody was watching it proactively — a stale token meant the
morning job silently starts hard-failing into MFA. `bot/jobs.py::_token_expiry_check_for_user`
runs unconditionally in the tick (pure decode, zero network) and DMs a heads-up once the
estimated `oauth1_expiry_est` is within `TOKEN_WARN_THRESHOLDS` (30, 7) days. The `bot_state`
guard (`token_warn:<threshold>`) stores the token's *issue date* as its value rather than a bare
flag — comparing against the current issue date means a fresh re-login (new issue date) makes
the stored guard stop matching and silently re-arms both thresholds, no explicit reset needed.
Best-effort: a missing/undecodable token blob is a silent skip, never a tick failure.
`tests/test_token_expiry.py`.

**Remote deploy from Telegram (OPS-03)**: the admin-only **`/deploy`** bot command — `git pull`
then restart the systemd services — for pushing code to the Pi without SSHing in. `app/deploy.py`
is the pure subprocess layer (no DB, no Claude): `git_pull()` runs `git pull --ff-only` in the repo
root (a diverged history fails loudly instead of silently creating an unwanted merge commit —
SSH in and sort it out by hand), `restart_services()` shells out via passwordless sudo to a
**transient systemd unit** running the **fixed** `scripts/restart_services.sh`, which itself runs
`systemctl restart --no-block garmin-bot.service garmin-web.service`.
**The cgroup-kill race** (found in production, not in review): an early version ran the script as
a *direct* `sudo` child — but that child lives in `garmin-bot.service`'s own cgroup, and the instant
`systemctl restart` queues the stop job, systemd's default `KillMode=control-group` SIGTERMs every
process in that cgroup, including the very child that just asked for the restart. `--no-block`
only shrinks that window, it doesn't close it — observed as an intermittent
`returncode == -15` (SIGTERM) with an empty output pipe, reported to the admin as a false "restart
failed" even though the restart had, in fact, just fired. The fix: `restart_services()` wraps the
script in `sudo systemd-run --unit=garmin-deploy-restart --collect ...` — `systemd-run` only opens
a short D-Bus round trip to register the transient unit and exits; the script then runs as a child
of PID1 in its **own** cgroup, never touched by garmin-bot's kill, so the confirmation this process
sends back is no longer racing its own death. Two deliberate choices survive from the original
design: (1) the sudoers grant (`deploy/sudoers-garmin-deploy`) whitelists that one fixed command
line, not a `systemctl`/`systemd-run` pattern — the script's contents decide what gets restarted,
not whatever a caller passes; (2) every `git_pull`/`restart_services` call is logged server-side
(`logger.info`, `"deploy"` logger — `journalctl -u garmin-bot`) with its return code and output
regardless of what reaches Telegram, since a killed/denied subprocess can come back with an empty
pipe and the chat message alone isn't enough to diagnose it after the fact.
`bot/handlers.py::deploy`/`deploy_callback`: `/deploy` checks `user.is_admin` (re-checked again in
the callback — defense in depth for a button tap) and the process-level `DEPLOY_ENABLED` master
switch (off by default — flip it on only once the sudoers file is installed), then asks for an
explicit ✅/❌ confirm (the same inline-button pattern as plan edits) before doing anything — a
mistyped `/deploy` should never silently restart production. On ✅: `git pull` runs first and its
output is shown; a failed pull (merge conflict, network) stops there and `restart_services` is
never called; a restart failure always shows the return code (never a bare, content-less
message); a successful one now gets an explicit "✅ Рестарт запущено" instead of trailing off in
silence — the confirmation is reliable precisely because it no longer races its own process being
killed. `tests/test_deploy.py`.

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

**Sick/travel mode (NF-03)**: a hard training plan snaps at the first week of flu — EP-02
eases a *day*, but "missed 5 days" was still manual `/plan` edits. `SYSTEM_SICK`
(`app/analysis/prompts.py`) + `run_sick_check`/`sick_with_stats` (`app/analysis/plans.py`,
built on the same `_plan_ops_with_stats` engine as edit/adapt/weather — CODE-06) propose a
*block rebuild*: skip the missed/near-term days, ease (modify) the sessions right after
return to easy/recovery, re-ramp by the usual ~10%/week rule — conservative, explicitly
non-medical wording (never diagnoses, never suggests medication). `_filter_sick_ops` is the
guard behind the prompt (same idea as `_filter_weather_ops`): only move/modify/skip survive,
dated within `today-SICK_LOOKBACK_DAYS..today+SICK_WINDOW_DAYS` (14 days back/forward) — the
model can propose freely, but a stray add/swap_exercise or a date outside the current block
never reaches the confirm buttons. Deliberately ignores the plan's `adjust_level` (ST-07):
illness is a reason to step outside the plan's normal adaptation bounds, not a candidate for
the "off"/"conservative" caps. Triggered by the **`/sick [днів]`** bot command
(`bot/handlers.py::sick`, optional "how many days already missed" argument, default 0 →
today/tomorrow easy) — reuses the existing `plan_callback`/`ctx.user_data["pending_plan"]`
✅/❌ flow (no new callback handler). `ReportLog(kind="sick")`; not dedup-cached (like the
other plan-ops calls). No automatic EP-08-style illness detector yet (the ticket names it as
a future trigger) — `/sick` is the only entry point today. `tests/test_sick.py`

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
app; no per-user column). EP-02 auto-deload integration is now wired — see NF-09 below.
`tests/test_injury.py`.

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
deactivated/disabled → silence). Fed into the daily report context — see ST-10 below.
`tests/test_health.py`.

**Health alerts in the daily report (ST-10)**: EP-08's own future-extension note — the report
already got `norm` (NF-01's raw percentile bands) but never the *detector's conclusion* ("this
metric has sat outside your band for 3+ days"). `run_analysis` (report/morning, not `/deep`) now
reuses the SAME 90-day history slice `norm` is built from (`health.detect`, zero extra DB read,
zero LLM) and, only when `level="alert"`, folds a compact `health_alerts` (`health.to_context` —
`{level, alerts:[{kind,severity,detail}]}`) into `user_content` **and `_cache_key`** (the README
pitfall). `SYSTEM`'s "СИГНАЛИ ВІДНОВЛЕННЯ" section tells the analyst to align tone with an alert
that already went out as its own DM, not repeat it as a second warning — the field is simply
absent while calibrating/none, so the prompt stays silent by default.

**Auto-deload from risk signals (NF-09)**: NF-04 and EP-08 detect risk and DM a warning, but the
plan itself never reacted — tomorrow's intervals stayed on the calendar regardless (NF-04's own
"future step" note). The morning tick now tries `bot/jobs.py::_deload_check_for_user` FIRST,
before the plain injury/health advisories: when `build_injury_assessment` is elevated/high or
`build_health_alerts` is actionable, AND a heavy session (tempo/intervals/long) sits within
`DELOAD_HEAVY_WINDOW_DAYS` (5) days, it calls the existing `run_plan_adaptation(..., trigger=
"deload", risk={...})` — a new optional `risk` param (`{"injury": injury.to_context(...),
"health": health.to_context(...)["alerts"]}`) that rides into the `SYSTEM_PLAN_ADAPT` prompt as
already-confirmed evidence (a new `trigger="deload"` rule: ease 5-7 days, cut harder on
`level="high"` or stacked signals, gentler on one weak signal — `adjust_level`'s bounds still
apply). Sends the same ✅/❌ confirm buttons as any other adaptation proposal
(`_send_adapt_proposal`). Reuses the injury guard (`INJURY_WARNED_KEY`, once per
`INJURY_GUARD_DAYS`) as the shared "one risk touchpoint per day" gate — a fired deload proposal
counts as that day's warning, so `_tick_for_user` skips the plain injury/health DMs when it fires
(`if not deload_sent: ...`); `_has_pending_proposal` is honoured like every other auto-proposer.
`plan_adapt_enabled=False` or `adjust_level="off"` → `run_plan_adaptation` itself returns
`(plan, None)` with zero Claude calls, same as any other adaptation trigger. Not dedup-cached
(adaptation never is). `tests/test_deload.py`.

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

**Multisport activity analysis (EP-10 phase 1)**: everything used to be run-centric by
construction — `fetch_activity_series` only ever pulled pace, `_segments`/`activity_payload`
only ever spoke min/km, so a ride's `/activity` analysis had no series and no sport-aware
language. Phase 1 (analysis only — phases 2–4 stay future work; phase 2's load-budget slice
already shipped separately as NF-05) generalises the run-only path to cycling: `client.
fetch_activity_series(activity_id, sport=)` now takes a sport bucket and reads different
Garmin `/details` descriptor keys per sport — running (default, unchanged shape)
`{d, p, hr}`; cycling `{d, spd, pw, hr}` (speed km/h from `directSpeed`, power watts from
`directPower` when the device reports it — `None` otherwise, never guessed). `app.garmin.
service._activity_rows` reuses NF-05's `multisport.sport_bucket` (rather than a second
keyword list) to decide whether/how to fetch series: `run` → `sport="running"`, `bike` →
`sport="cycling"`; swim and other buckets get no series in this phase (the ticket's own
pitfall note: Garmin's pool/open-water swim metrics are specific enough — SWOLF, pool
length — to deserve their own pass, not a bolt-on). `reports._segments` collapses whichever
keys are present into per-segment averages (`avg_pace`/`avg_hr` for a run, `avg_speed_kmh`/
`avg_power_w`/`avg_hr` for a ride) instead of hard-coding pace+HR; `activity_payload` picks
`avg_speed_kmh` vs `avg_pace` by `sport_bucket(activity.type)`, not by sniffing the series
shape. `SYSTEM_ACTIVITY` gained a short cycling-specific data section + an instruction not
to convert `avg_speed_kmh` into a pace. The web detail-page chart (`app.charts.run_charts`,
shared by `/ui` and `/me` since EP-04) picks a speed/power pair of sparklines over pace when
the series carries `spd`/`pw`, with matching hover-tooltip formatting in `detail.html`
(`fmt="speed"|"power"`). Running behaviour, cache keys (`series:v1:<id>`, stable — a given
activity's sport never changes) and existing tests are all unchanged. `tests/
test_activity_series.py`.

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

**Quarterly/yearly Wrapped (NF-07)**: a season of training given a shape — a celebratory recap.
`app/wrapped.py` is the **pure-Python** part (mirrors `compare.py`): `period_window(today, kind)`
picks the trailing window (`year`=52 weeks / `quarter`=13 weeks, rolling so it's never empty),
`parse_period` reads the `/wrapped [рік|квартал]` arg, `has_signal` bails on a near-empty window,
`label`/`fmt_range` render the header, `build_context` shapes the payload. `repository.wrapped_stats`
**reuses `window_stats`** (no duplicate volume math — the CODE-02 lesson) and augments it with the
whole-period extras a recap wants: an all-sport activity breakdown (via `multisport.sport_bucket`) +
total hours, the biggest running week, and the VO2max arc (first vs last); `records_in_range` pulls
the milestones set in the window. `service.run_wrapped` narrates **one aesthetic Opus longread**
(`SYSTEM_WRAPPED`, `MODEL_WRAPPED`=Opus — rare, so the cost is fine), dedup-caches on the period +
stats + records (`_wrapped_cache_key`), logs `ReportLog(kind="wrapped")`, returns `None` on an empty
window. Surfaced as the **`/wrapped [рік|квартал]`** bot command (pure DB read + one Opus call, no
Garmin/MFA). `tests/test_wrapped.py`.

**Correlation engine (NF-02)**: "what actually affects you" — a **pure-Python, zero-LLM** monthly
pass over the recovery history in the DB (`app/correlations.py`). Tests a fixed set of lagged metric
pairs (sleep→next-day HRV, stress→HRV, resting-HR→HRV, …) for a real personal association and keeps
only the **statistically defensible** ones: `find_correlations(history)` gates each pair on a minimum
sample count (`MIN_SAMPLES`=30), a meaningful effect size (`|r|` ≥ `R_THRESHOLD`=0.35) **and** a
Fisher-z 95% CI that excludes zero (`_fisher_ci_excludes_zero` — numpy-free significance) so noise on
thin data is filtered, not surfaced. `pearson`/`_paired` (lag-aware, indexes by ISO date so a backfill
gap drops a pair rather than misaligning). Nothing significant → an honest `None` (no Claude call).
`service.run_insights` narrates the survivors via **one Sonnet call** (`SYSTEM_INSIGHTS`,
`MODEL_INSIGHTS`, cautious — correlation≠causation), dedup-caches on the findings (`_insights_cache_key`),
logs `ReportLog(kind="insights")`. Surfaced two ways: the **`/insights`** bot command (pure DB read +
at most one Sonnet call) and a **monthly auto-block** riding the first weekly digest of the month
(`_monthly_insights_for_user` in `bot/jobs.py`, guarded via `bot_state` `insights:<YYYY-MM>`, set only
after a send — same pattern as NF-06). `tests/test_correlations.py`.

**Web dashboard (EP-04)**: `GET /dashboard` (`app/routers/dashboard.py`) is a single
mobile-first overview page replacing "page through the raw `/me` tables" as the product
home — a **pure DB read**, zero Garmin/Claude calls, so it renders in well under 100ms.
Reuses building blocks rather than growing a parallel stack: the "today" hero ring is
`me._latest_ring` (the same readiness/sleep-score ring as `/me`), the 30-day HRV/RHR/
sleep/stress sparklines are `app.charts.trend_series` (hover-enabled), the plan-week rows
mirror `/plan`'s markup (`plan._dow`/`_dm` filters), and the activity cards reuse
`me._act_meta`/`_pace_str`. `app/charts.py` is new: the inline-SVG chart primitives
(`series`/`trend_series`/`run_series`/`run_charts`) were extracted out of
`app/routers/admin.py` (and a near-duplicate `me.py::_trend_series`) into one shared
module — `admin.py`/`me.py` now import from it with zero behaviour change. The one new
repository read is `repository.month_cost(session, user_id)` — `SUM(cost_usd)` since the
start of the current calendar month (UTC); EP-06's future quota work reuses it as-is.
Empty states (no history / no plan / no activities) render a short note linking to
`/settings`/`/plan` instead of a blank page. The post-login redirect for a non-admin
(`routers/auth.py::login_submit`) and the logged-in root `/` (`app/main.py`) now point at
`/dashboard` (admins still land on `/ui`). PWA-minimum: `app/static/manifest.json` +
`app/static/icon.svg`, linked only from `dashboard.html` (no app-wide `<link>` — there's no
single base template to hang it off). `tests/test_dashboard.py`.

**Personal MCP server (NF-08, experiment)**: `app/mcp_server.py` is a thin **read-only**
stdio MCP wrapper (opt-in dependency, `pip install -e ".[mcp]"`) around the exact same
user-scoped, read-only tools EP-09's `/ask` agent uses —
`query_activities`/`query_daily`/`aggregate_weekly`/`get_activity_detail`/
`get_training_plan` — all funneled through the single dispatch point
`app.analysis.reports._run_ask_tool` (same row caps, same whitelisted daily fields, zero
duplicated validation). Single-user process: `--email` resolves to a `user_id` once at
startup (`_resolve_user_id`); every tool call opens a fresh DB session. Zero Garmin calls,
zero LLM cost on our side (the MCP client's own subscription pays for inference) — "talk to
your own data" from Claude Desktop/Code without the bot/web UI. Run:
`./venv/bin/python -m app.mcp_server --email me@example.com`, then point a client's MCP
config at that command. Deliberately kept read-only — NF-08's own ticket names scope creep
as the main risk, so there's no write tool here and none should be added.
**Every `mcp` release requires Python >=3.10** — it cannot install into the project's
Python 3.9 baseline (the Pi/CI target), so this is meant to run from a separate 3.10+
venv (wherever the MCP client lives), not necessarily on the Pi itself; the test module
(`tests/test_mcp_server.py`) skips itself via `pytest.importorskip("mcp")` when the extra
isn't installed, so CI (3.9, `.[dev]` only) stays green without it.

**Web chat with the coach (EP-11)**: `GET/POST /chat` (`app/routers/chat.py`) — one input
box in the web UI, backed by the exact same engines as the bot's `/ask` and `/plan <text>`
(`run_ask` from EP-09, `run_plan_edit`), not a parallel implementation. A tiny keyword
heuristic (`_looks_like_plan_edit` — imperative verbs like "перенеси"/"додай"/"заміни") picks
the engine per message; a miss just falls through to `run_ask`, which can still answer a
question ABOUT the plan via its own `get_training_plan` tool, so there's no dead end. Chat
history needs no new table: `repository.get_chat_history` reads straight off `ReportLog`
(`kind in ("ask", "plan_edit", "sick")`) — user-scoped, not chat-scoped, so a question asked
in Telegram already shows up in the web transcript and vice versa, for free.

**Shared DB-backed pending-plan-edit state** (the AC this epic actually turned on): the
free-text `/plan <text>`/`/sick` confirm flow used to stash its ✅/❌ ops in Telegram's
`context.user_data["pending_plan"]` — in-memory, per-process, Telegram-only, and gone on a
bot restart (unlike EP-02's adaptation proposals, which already lived in `bot_state` via
`PENDING_ADAPT_KEY` for exactly this reason — they can be sent by a background job, not a
live chat turn). `repository.set_pending_plan_edit`/`get_pending_plan_edit`/
`pop_pending_plan_edit` generalise that same `bot_state` pattern under its own key
(`PENDING_PLAN_EDIT_KEY`, so an in-flight free-text edit never collides with an outstanding
adapt/weather/deload proposal) — `set` stores `{ops, alt, summary, alt_summary, risky}`
(the display extras are new: a Telegram message already has its text baked in, but the web
page has to re-render the proposal on every GET, across reloads); `pop` is single-use (a
stale button reads back nothing); `get` peeks without clearing, for re-rendering. `bot.
handlers._plan_edit`/`sick`/`plan_callback` now read/write through these helpers instead of
`context.user_data`, so a proposal shown in the bot can be confirmed from the web chat and
vice versa, and survives a bot restart — `plan_callback` itself is otherwise unchanged
(same apply + best-effort Garmin resync). `POST /chat/confirm` on the web side mirrors
`plan_callback` almost line for line: pop the pending ops, `repository.apply_plan_ops`,
best-effort `plan_sync.resync_workouts` if `garmin_sync_enabled`. HTML buttons (not JS) —
✅ apply / 🛡 take-the-safer-suggestion (when `risky` + an alt exists) / ❌ cancel, same
three-way shape as the bot's risky-edit buttons.

**Deliberate v1 scope, documented not silently dropped**: responses are NOT token-streamed.
The ticket's SSE AC would mean moving the Anthropic client off the dedicated sync
threadpool PERF-04b deliberately chose (see the Concurrency section above) onto
`AsyncAnthropic` — a materially larger, separate change than this router, so it's left for
a follow-up. Every turn is a plain POST + full-page reload; there is no JS-only fast path to
degrade *from*, so the "still works without JS" AC holds by construction rather than by a
progressive-enhancement fork (contrast EP-04/ST-05's hover-JS additions, which do have one).
`load_credentials` (not `user_runtime`) is used for the `/ask`/edit calls themselves — pure
DB + Claude, no MFA risk, same as the bot's `/ask`/`/compare`; only the confirm step's
Garmin resync binds `user_runtime`, same MFA exposure the bot's `plan_callback` already had.
`/sick`'s medical-safe framing stays bot-only (not wired into the chat router) — a
documented, deliberate exclusion, not a gap. `tests/test_chat.py` +
`tests/test_repository.py::test_pending_plan_edit_*`/`test_get_chat_history_*`.

**Per-user timezone (ST-14)**: `User.timezone` (IANA string, default `Europe/Warsaw`,
migration `a3b4c5d6e7f8`) + a `/settings` field validated via `zoneinfo.ZoneInfo` on save
(a bad string → `?tz=fail`, never a 500). `bot.jobs.user_tz(user)` is the shared reader —
falls back to the process `TZ` on a corrupt/missing value so a bad zoneinfo string can
never break a job. **Per-user checks** (not the job schedule) read it: `_tick_for_user`
used to receive a `now`/`today` computed once for the whole batch in `morning_job`
(so a single global 07-23 window check gated everyone); it now computes both itself from
`user_tz(user)` and does its own window check, so a traveling user or a second user
outside CET gets their own morning, not the process's. The once-a-day/week/month
`bot_state` guard dates that key off "today" (`digest:<iso-week>`, `compare:<YYYY-MM>`,
`insights:<YYYY-MM>`, the morning/injury/health/adapt/extend guards) inherit this
automatically since they're computed from the same per-user `today`. **Deliberate v1
scope**: the `run_daily`-scheduled jobs' own firing hour (digest hour, plan-sync hour,
weather-plan hour, sleep-nudge hour, adapt-review hour) stays on the process TZ — repointing
those per-user needs re-registering a `JobQueue` job per user, a bigger change reserved for
when a user outside ~±2h of Europe/Warsaw actually shows up (documented in the ticket).

**`/costs [YYYY-MM]` (ST-12)**: `repository.costs_for_month(session, user_id, start, end)`
aggregates `report_logs` over a caller-supplied `[start, end)`: total $, a per-`kind`
breakdown (`{cost, calls}`), total/cache-hit call counts, and the 3 priciest individual
calls (a `cached=True` row counts toward `calls` — visible cache effectiveness — but never
appears in the top-3, since its cost is ~$0). `bot/handlers.py::costs_cmd` is a pure DB read
(no Garmin/Claude, like `/records`/`/compare`) that computes the month boundary in the
user's OWN timezone (`bot.jobs.user_tz`, ST-14) rather than UTC — "this month" means their
month. `/costs 2026-06` targets an explicit month; garbage input gets a format hint instead
of a stack trace.

**Weather chips on `/plan` (ST-13)**: `app/routers/plan.py::_weather_chips` is a best-effort
helper (same live-fallback shape as `_strength_details`) that, given a stored location, pulls
`weather.fetch_forecast_week` (one `run_in_threadpool` fetch per render, no cache in v1 —
Open-Meteo is free and fast) and reuses the exact same pure `weather.find_weather_conflicts`
that `weather_plan_job` (EP-13) uses to decide whether to propose a move. The plan page only
**shows** why a session might get a move proposal (🌡️ feels-max / 🌧️ rain-prob / 💨 wind
chips, a conflicting day highlighted via the `wx-conflict` CSS class) — it never itself calls
Claude or proposes anything; that stays the job's one-proposal-at-a-time territory. Only the
**active** plan (`GET /plan`) gets chips — the read-only `/plan/{id}` view (an archived plan)
never does, since a past forecast means nothing. No location or a failed fetch → the page
renders exactly as before, just without the chips section.

**Heat/duration fueling advisor (NF-11)**: EP-13 already moves a key session off an
extreme-weather day; it never said how to survive one that stays. `app/fueling.py` is a
**pure-Python, zero-LLM** calculator: `estimate_minutes` derives a session's duration from
its structured `steps` (a small self-contained recursive estimator — deliberately NOT
imported from `app.routers.plan`'s near-identical one, since a web router shouldn't be a
dependency of a core module), else `dist_km` at an anchor pace, else a rough per-`type`
floor. `advise(session, forecast)` — called ONLY for **today's** session (the ST-03/EP-13
proximity rule: no gel math for Friday) once it clears `FUELING_MIN_DURATION_MIN` — returns
fluid (mL/h) past `FLUID_DURATION_MIN`, carbs (g/h) past `CARB_DURATION_MIN`, and, when
`feels_max_c` is at/above `FUELING_HEAT_FEELS_C`, an electrolyte note plus the coolest
hourly forecast slot. Wired into `run_analysis` (report/morning, not `/deep`) with **zero
extra Claude/network calls**: it reuses the SAME `weather` dict ST-03 already fetched and
the day's `plan_today` entry, folding a compact `fueling` snapshot into `user_content`
**and `_cache_key`** (the README pitfall) only when there's something to say — a short/easy
session, no forecast, or a cool short session all leave the context key simply absent (the
`norm`/`records` pattern). `SYSTEM`'s new "ХАРЧУВАННЯ/ГІДРАТАЦІЯ" section tells the analyst
to voice the ready numbers, not invent its own.

**Evening sleep-debt nudge (NF-16)**: the whole product reacted only in the morning, once a
bad night was already spent. `app/sleepnudge.py` is a **pure-Python, zero-LLM** detector,
fired from a new evening job the night BEFORE a heavy session: `has_sleep_debt` reuses NF-01's
`baselines.compute_baselines` as the threshold (sleep_h below the personal band on ≥2 of the
last 3 nights) OR Garmin's own `sleep_need_h` (now carried in `repository.read_history`'s
`extra` field) outpacing actual sleep by `NEED_GAP_H` — a signal even before there's enough
history for a personal band, so a brand-new user isn't silent by default; `tomorrow_is_heavy`
checks the active plan's next-day session type. **Both** conditions must hold — either alone
stays silent (EP-13's "no conflict, no message" rule extended to a third detector) so this
never nags before every tempo run. `bot/jobs.py::sleep_nudge_job` (`run_daily` at
`SLEEP_NUDGE_HOUR`=21, Europe/Warsaw — the job's firing hour is process-TZ in v1, ST-14)
loops via `for_each_user`; `_sleep_nudge_for_user` computes "today"/the once-a-evening
`bot_state` guard (`sleep_nudge:<date>`) in the user's OWN timezone. Toggle: reuses
`User.alerts_enabled` (the same wellness-push class as EP-08) plus the process-level
`SLEEP_NUDGE` switch. **Deliberate v1 limitation**: no specific bedtime clock time — nothing
currently stored gives a wake-time to count back from, so the nudge says "lie down earlier"
without a number (the ticket itself names this fallback as acceptable).

**Race pack (EP-05)**: `TrainingPlan.target_date` was already a typed ISO string (phase 0
turned out to be nearly free) — what was missing was a typed **target distance**:
`app/race.py::GOAL_DISTANCE_KM` maps a race goal (`first_5k`/`faster_5k`/`first_10k`/
`first_half`) to a km number, sibling to `app.goal.GOAL_METRIC` (which maps a goal to the
Garmin *prediction* metric, not a fixed distance) — the open-ended `general` goal has
neither (`race.has_target(plan)` gates everything on both being present). `run_race_plan`
(`app/analysis/reports.py`, mirrors `run_compare`/`run_wrapped`'s shape) assembles the
context in Python — a fitness snapshot (`get_recent_extra`), the plan's own upcoming
sessions through race day (`recent_sessions` — the ALREADY-DECIDED taper; the model is told
to reference it, never propose a different one), and the target date's forecast (reusing
EP-13's `fetch_forecast_week`, only within `race.WEATHER_WINDOW_DAYS`=7) — and narrates ONE
Opus call (`SYSTEM_RACE`, `MODEL_RACE`) into target/backup pace, a distance-appropriate
splits/negative-split breakdown, fueling by minute (≥half only), a pre-race checklist, and a
weather note when relevant. Dedup-cached (`_race_cache_key`) and `ReportLog(kind="race")`.
Surfaced two ways: the **`/race`** bot command (on demand) and the **daily**
`plan_sync_job` auto-trigger (`bot/jobs.py::_race_pack_for_user` — not the 20-min tick; a
live weather/Opus call doesn't need that cadence), which fires exactly `race.TRIGGER_DAYS`
(7) days before `target_date`, guarded **per-plan** (`bot_state` `race_pack_sent:<plan_id>`)
rather than per-date so a missed tick can't lose the trigger and a fresh plan/target
naturally re-arms it. `/plan` shows the last generated pack as a standing block while the
race is within `race.PLAN_BLOCK_DAYS`=14 (`repository.get_last_report_of_kind` — a new
generalisation of `get_last_report` to an arbitrary `kind`) — the block only ever reads the
last report, it never itself calls Claude. `tests/test_race.py`.

**User data export (NF-13)**: `GET /me/export` (`app/routers/me.py::me_export`) streams a
ZIP of everything an account owns — `daily_metrics`/`activities` as JSON (full fidelity:
`extra`/`series`/`subjective`/`steps` intact) **and** flat CSV twins for Excel/Sheets,
`personal_records.json`, `plans.json` (plan + its `PlannedWorkout`s incl. `steps`), and
`report_logs.json`. Explicit per-model column allowlists (not `__table__.columns`) are the
safety net: the `users` row is **never read** by this route at all, so credentials/garth
token/password hash can't leak by construction, not by careful filtering. Registered
**before** the parameterised `GET /me/{table}` route (same router) so the literal path wins
first. Pure DB read scoped to `user.id`, zero Garmin/Claude calls — linked from `/me` as
"⬇️ Експорт даних". Deliberately not a disaster-recovery mechanism (that's OPS-02's DB
backup) — this is portability. `tests/test_routers.py` (cross-user isolation, no-secrets
grep over the raw ZIP bytes, empty-history still a valid ZIP).

**Shoe mileage tracker (NF-15)**: `app/gear.py` is a **pure-Python, zero-LLM** module —
unusually for this codebase, its ticket's own AC #1 flags live endpoint verification as **a
blocker, not a detail**, and this session had no live Garmin account to verify against (see
the module's own docstring and `app/garmin/client.py`'s GEAR section for the full recon
trail: the community `python-garminconnect` library exposes `get_gear`/`get_gear_stats`
[`/gear-service/gear/filterGear?userProfilePk=`, `/gear-service/gear/stats/{uuid}`] but
**no** activity→gear link endpoint at all). Two deliberate deviations from the ticket's
original design follow from that gap: (1) mileage comes straight from Garmin's own
per-gear `stats` total (already shown as a shoe's lifetime distance in the Connect UI)
instead of summing our own `ActivityRecord` rows by a `gear_id` we'd have to backfill and
could easily get wrong — no migration column, no backfill CLI; (2) every parse
(`gear.parse_item`/`parse_mileage_km`/`parse_last_used`) is defensive against an
unconfirmed shape — logs once (`GEAR ... shape unrecognised`) and returns "no data" rather
than guessing, so a wrong field name can only under-report, never send a false warning.
**Not yet independently live-verified** — treat the numbers as provisional until confirmed
against a real account. `bot/jobs.py::_gear_check_for_user` runs from the **daily**
`plan_sync_job` (not the 20-min tick — a live gear fetch isn't cheap enough for that
cadence), refreshes the roster+mileage into a `bot_state` JSON blob (`gear.STATE_KEY`) so
the **`/gear`** command is a plain DB read afterwards, and warns once per pair past
`GEAR_WEAR_KM` (700, `0` disables) via `bot_state` `gear.WARN_PREFIX + gear_id` — storing
the mileage AT the warning (not just a flag) so `gear.should_rewarn` can nudge again every
further `GEAR_REWARN_KM` (150) instead of nagging daily or going silent forever. Retired
gear never warns; `gear.dominance_note` flags the "only one pair actually tracked" honesty
case from the ticket's own pitfall. `tests/test_gear.py`.

**Seasonal multisport intake (NF-12)**: NF-05 made cross-sport load *visible* after the
fact ("last week was heavy on kite — ease off running"); generation itself stayed
run-centric. `intake["season"]` (`{sport, sessions_per_week, avg_min}`, picked on the
`/plan` setup form — kite/tennis/bike/other, entirely optional) is a **declared-ahead**
seasonal accent, not a fact like NF-05's `multisport` — the two ride as separate context
keys (`season` vs `multisport`) so `SYSTEM_PLAN`/`SYSTEM_PLAN_ADAPT` can tell "planned
intent" from "measured load" apart; the ADAPT prompt explicitly says not to "give back" the
volume generation already reserved for a stated season just because compliance looks
lighter than usual. Wired into `run_plan_generation`, `run_plan_extension` and
`run_plan_adaptation` (`app/analysis/plans.py` — all three already had `intake`/`plan.intake`
in scope) as `context["season"] = intake.get("season")`; neither generation nor adaptation
is dedup-cached, so no cache-key wiring needed. No day-of-week binding in v1 (a kite day
floats with the wind) — pure weekly budget, matching the ticket's own scope note. Changing
the accent doesn't require regeneration: **`POST /plan/season`** (mirrors ST-07's
`/plan/adjust-level`) reassigns `plan.intake` directly; an empty `season_sport` clears the
key entirely (`app/routers/plan.py::_parse_season`). `tests/test_season.py` +
`test_routers.py::test_plan_season_*`.

**Models**: `/report` + morning + `/ask` + `/activity` + weekly digest use `claude-sonnet-5`; `/deep`,
**training-plan generation** (`MODEL_PLAN_GEN` — reasoning-heavy + infrequent, so the
cost is fine) and the **race pack** (`MODEL_RACE`, EP-05 — a rare, once-per-race synthesis)
use `claude-opus-4-8`. Plan **edits** (`/plan <text>` → ops) stay on Sonnet
(`MODEL_PLAN`) — small and mechanical. Plan generation also accepts a **Fable** engine via
the setup-form toggle (see the strength/plan section). Every call is logged to `ReportLog`
(tokens, cost, ok/error). `PRICES` (Anthropic list prices, $/1M in/out): Sonnet 5 **intro**
$2/$10 through 2026-08-31 (bump to $3/$15 on 2026-09-01), Sonnet 4.6 $3/$15, Opus 4.8
$5/$25, Fable 5 $10/$50. NB Sonnet 5 uses the newer tokenizer (~30% more tokens for the
same text than Sonnet 4.6), so per-request token counts rise.

**`/ask <question>` (EP-09): a bounded tool-use agent over the FULL stored history** — the
project's **first SDK tool-use** (a deliberate reversal of the earlier "prompt-for-JSON,
no SDK tool-use" choice noted below for plan gen/edit/adapt, which stays as-is; `/ask`
alone needed open-ended multi-step lookups a single JSON schema can't express).
`run_ask` seeds the loop with the last `ASK_DEFAULT_N` (3) **daily** reports
(`repository.get_recent_reports`, filtered to `kind="report"`) **plus** `get_recent_asks`
— this user's `/ask` exchanges from the last `ASK_CONTEXT_MIN` (30) minutes — so an
in-context follow-up answers in one round, no tool calls. Anything needing more drives
`run_ask_agent` (`app/analysis/reports.py`): up to `MAX_ASK_ROUNDS` (5) round trips of
`client._complete_tools` (Sonnet, `SYSTEM_ASK_TOOLS`) against five **read-only,
user-scoped** tools (`_ask_tools()`, dispatched by `_run_ask_tool`) — `query_activities`/
`query_daily` (date-range reads, capped at `repository.ASK_MAX_ROWS`=200 rows,
`query_daily` restricted to the `ASK_DAILY_FIELDS` whitelist so a typo'd field is a silent
miss, not an arbitrary-column fishing trip), `aggregate_weekly` (one metric bucketed per
ISO week — run-volume via `weekly_run_volume` or any `ASK_DAILY_FIELDS` name averaged per
week), `get_activity_detail` (reuses `activity_payload` — segments, not the raw series),
and `get_training_plan` (`repository.query_training_plan` — the active `TrainingPlan`'s
goal/target/summary plus its dated sessions in a range; without it, a question about the
*program itself* — upcoming sessions, goal, adherence — had nothing but a stray mention in
a report's text to go on; the prompt tells the model this tool is plan, `query_activities`
is fact, don't conflate them). A tool never raises; a bad name/arg/DB hiccup becomes
`{"error": ...}` the model can react to. `MAX_ASK_TOTAL_TOKENS` (60k combined in+out) is a
second, cost-based cutoff independent of the round count. Hitting either limit while still
mid-tool-use returns `ASK_LIMIT_TEXT` (an honest "уточни питання") instead of a
partial/guessed answer — never
raised as an error. Dedup-cached on the question + `repository.latest_daily_date` (a
pure-DB, no-Garmin "how fresh is the data" proxy — see `_ask_cache_key`); logs a
`ReportLog(kind="ask", tool_rounds=<n>)` so a multi-round question's real cost is visible
(`tool_rounds` is null for every other `kind`, and for a cache hit). Bot-only (no web
endpoint — deliberately kept out of the web app); pure-DB (`load_credentials`, not
`user_runtime`, like `/compare`), so an MFA gate never blocks a question.

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

**Step-level plan-vs-actual (NF-14)**: EP-01 only matches a session as a whole ("the tempo run
happened") — but structured workouts push per-interval pace targets to the watch
(`PlannedWorkout.steps` → `workout_export.build_workout`), and nobody checked whether the runner
actually hit them ("did 8x400" can mean nailed every one or blew up after the third — opposite
adaptation signals). `app/stepmatch.py` is **pure, zero-LLM**: `flatten_steps` expands the steps
tree into the exact order the watch executes them (a `repeat` block's children appear `reps`
times in a row; the container itself is never a lap, mirroring `workout_export._build_steps`'s
numbering), `match` pairs that sequence positionally against the activity's actual laps
(`client.fetch_activity_splits`, `/activity-service/activity/{id}/splits`, disk-cached
`splits:v1:<id>`, immutable) and scores each **working** step (run/tempo/interval with a pace
target) hit/miss with a small tolerance (`PACE_TOLERANCE_PCT`/`PACE_TOLERANCE_MIN_KM`) —
warmup/recovery/cooldown steps still occupy a slot (for index alignment) but are never a
"working" miss; fewer laps than steps (stopped early) scores the un-lapped work as an honest
partial (`actual: null`). Gated on the activity being matched (EP-01) to a session **we actually
pushed with structure** (`garmin_workout_id` + `steps` both set) — an unpushed or freeform run
stays silently `None` rather than guessing. Computed once in the morning tick right after
matching, before the auto-analysis (`bot/jobs.py::_step_match_for_activity`, idempotent,
best-effort — a Garmin/parse hiccup never blocks the analysis), stored on
`ActivityRecord.step_match` (migration `e48815b991f2`). Three consumers: `activity_payload`
feeds it to `SYSTEM_ACTIVITY` (and automatically enters `_activity_cache_key`, which hashes the
whole payload), a `"🎯 6/8 у цілі"` badge (`stepmatch.badge`) in the auto-analysis DM and the
`/me` activity detail page, and `stepmatch.aggregate`/`repository.recent_step_match` feed a
compact hit-rate summary into `run_plan_adaptation`'s context — a systematically missed pace
target is a *calibration* signal (the plan's target paces are off), not the same thing as a
missed session. `tests/test_stepmatch.py` + `test_step_match_job.py`.

**Goal progress projection (NF-10)**: the weekly digest could only say "відстаєш" qualitatively
— nothing put a number on it. `app/goal.py` is **pure, zero-LLM** (mirrors `compare.py`/
`wrapped.py`'s shape): `weekly_medians` smooths Garmin's daily-jitter race-time predictions into
one median per ISO week (the same "trust weekly, not daily" reasoning EP-14 uses for records),
`_linear_trend` fits a numpy-free least-squares line over week ORDER (not calendar week number,
so a backfill gap just shortens the series instead of skewing it), and `project` extrapolates to
the plan's `target_date` **only when it's within `FAR_HORIZON_WEEKS` (12)** — an honest refusal
to promise a number too far out. `metric_for_goal(plan.goal)` maps a race goal to Garmin's
matching prediction (`race_5k_s`/`race_10k_s`/`race_half_s`) or falls back to `vo2max`
(`higher_better=True`) for the open-ended `general` goal, which has no target race at all.
`repository.read_fitness_history` keeps the raw per-day series (unlike `get_recent_extra`, which
coalesces to one latest snapshot) so the trend has real weekly buckets to fit. A `verdict`
(on_track/close/behind) only appears once a `target_s` exists — the plan-setup form has **no
time-target input today**, so `/goal` is honestly trend-only in practice (a documented,
deliberate limitation, not a bug — `target_s` is wired for when that input lands).
`goal.summary` is a deterministic Ukrainian formatter (**zero Claude calls** in this version).
Surfaced two ways: the **`/goal`** bot command (pure DB read, no Garmin/MFA risk) and a
`goal_projection` block in the weekly digest context (**and `_digest_cache_key`** — the README
pitfall) that `SYSTEM_DIGEST`'s "чи на треку до цілі" section now leads with instead of eyeballing
raw race predictions. `tests/test_goal.py`.

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
choice avoids SDK tool-use here — unlike `/ask` (EP-09, above), which needs open-ended
multi-step DB lookups a single JSON schema can't express; every other `messages.create`
call in the app (plan gen/edit/adapt/weather/strength, all narration) stays prompt-only.
**Open-ended "keep improving" plans**: a fifth goal `general` (`GOALS`/`OPEN_ENDED_GOALS`
in `routers/plan.py`, `plans.OPEN_ENDED_GOAL`) with **no target race** — a rolling plan you
just keep running. Generation lays a first block of `PLAN_BLOCK_WEEKS` (6) weeks: the plan
is stored with `target_date=None` (so all the `target_date`-guarded logic treats it as
open — adjust-level defaults flexible, no taper), while the model gets a concrete block-end
as its range plus an `open_ended` flag (`SYSTEM_PLAN`: no подводка, sustainable progression
with room to grow). Extending is **confirm-only** (never auto-generated — Opus costs money):
the **morning tick** hook `bot.jobs._extend_nudge_for_user` (right after `_adapt_morning_check`,
in-window) sends a ✅/❌ nudge when the plan's last workout is within `PLAN_EXTEND_LEAD_DAYS`
(10) — pure DB reads, zero Claude calls, guarded once/day (`bot_state extend_nudge:<date>`).
A ✅ (`bot.handlers.plan_extend_callback`, callback `planext:yes`) runs
`analysis.plans.run_plan_extension` on demand: it **appends** the next 6-week block to the
SAME plan (`repository.append_workouts` — never archives/regenerates), continuing progression
from the tail (`previous_weeks` context) and rebasing `week` numbers (`week_offset` = current
max week); it re-checks the plan is still near-end first, so a stale button never double-spends.
A ❌ (`planext:no`) snoozes the nudge for `PLAN_EXTEND_SNOOZE_DAYS` (3, `bot_state extend_snooze`);
an ignored nudge just re-asks next morning. Strength is extended too, reusing the first block's
custom sessions + re-cloning saved templates (`_add_plan_strength(..., reuse_only=True)`,
windowed via `add_strength_workouts`'s `start`/`end`/`week_offset`) — **no extra Claude call**.
Best-effort Garmin re-sync after the ✅. **Cost note**: the extension is the one path that
fires a real Opus call from a bot interaction, but only ever after an explicit ✅ tap — the
morning nudge itself is free, and there's no scheduled auto-generation.

## Caching layers

- **Claude dedup** (`llm_cache` table, PERF-02): keys on a hash of the meaningful
  payload (`daily`, `recent_activities`, `planned_runs`) + date + question + model +
  `previous_report`. The volatile `generated` timestamp is deliberately excluded —
  otherwise the key changes every minute and never hits (the main gotcha if you touch the
  key logic). `/ask` (EP-09) keys instead on the recent reports + `recent_qa` thread +
  question + model + `last_data_date` (a coarse daily-data-freshness proxy — see the
  `/ask` section above). 1-week TTL; expired rows purged lazily on write. Hit logs
  `CLAUDE CACHE HIT`.
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
