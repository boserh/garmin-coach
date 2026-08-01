# Design notes

Per-feature mechanism notes and gotchas, split out of `CLAUDE.md` to keep that file to
hard rules + architecture + current operational state. Reference this file when
touching one of these features; ticket codes (EP-*/NF-*/ST-*/OPS-*) are backlog IDs, not
file paths.

## Garmin provider (OPS-10: native `python-garminconnect`)

`gconn` (native client, curl_cffi TLS impersonation) is the default engine; `garth`
(deprecated upstream, Cloudflare-fingerprinted) is a rollback extra
(`pip install -e ".[garth]"` + `GARMIN_PROVIDER=garth`). Endpoint URLs and `client.py`
are unchanged — the provider interface (`login()`/`connectapi()`/`username`/
`display_name`) was designed for exactly this swap. Three things the new engine forced:
- **`connectapi` is GET-only** — `providers._gconn_connectapi` translates a stray
  `method=POST/PUT/DELETE` into the client's `post`/`put`/`delete` calls.
- **Session format changed, not convertible** (native = plain JSON `di_token`/
  `di_refresh_token`/`di_client_id` vs garth's base64 `[oauth1, oauth2]`) —
  `providers.is_gconn_token` tells them apart; a garth-token user gets one silent fresh
  login (logged INFO, not the scary WARNING).
- **`new_token` is computed, not assigned** — `_UserGConnProvider.new_token` is a
  property diffing the current dump vs loaded (native client refreshes DI token
  in-place); no cached `profile` dict any more (`username`/`display_name` lazy-fetched).

Auth failures stay monitored via grep-stable markers `GARMIN AUTH FAIL` (ERROR, fresh
login failed) and `GARMIN AUTH: stored token resume failed` (WARNING). PERF-05's rate
limiter/429 backoff live above the provider in `client._api`, unaffected by the swap.
Python 3.12+ is now required (Pi 3.13.5, CI 3.13).

## Token-expiry warning (ST-11)

`bot/jobs.py::_token_expiry_check_for_user` runs unconditionally in the tick (pure
decode, zero network), DMs a heads-up when `session_expiry_est` is within
`TOKEN_WARN_THRESHOLDS` (30, 7) days. Guard key stores the token's *issue date* (not a
bare flag) — a fresh re-login re-arms both thresholds automatically. Best-effort: a
missing/undecodable token, or a native refresh token that isn't a JWT, is a silent skip.

## Separate system/admin bot

`/deploy` (OPS-03) and `/test_*` live on a **second Telegram bot**
(`bot/admin_main.py` → `garmin-admin-bot.service`), off the main coaching bot. Same
handler code, codebase, DB — only token/process differ. Gated by `_owner_only` (locked
to the lowest `users.id`). `deploy/garmin-admin-bot.service`; `scripts/
restart_services.sh` restarts all three units (`garmin-bot`, `garmin-web`,
`garmin-admin-bot`).

## Remote deploy from Telegram (OPS-03)

`app/deploy.py`: `git_pull()` (`git pull --ff-only` — a diverged history fails loudly,
never an unwanted merge), `restart_services()` shells to passwordless sudo running
`scripts/restart_services.sh`. **The cgroup-kill race** (found in prod): running the
script as a direct `sudo` child put it in `garmin-bot.service`'s own cgroup, so the
restart's SIGTERM killed the very child that requested it — intermittent false "restart
failed" with return code -15. Fixed by wrapping in `sudo systemd-run --unit=garmin-
deploy-restart --collect ...` so the script runs as a PID1 child in its own cgroup.
`bot/handlers.py::deploy`/`deploy_callback`: admin-only, `DEPLOY_ENABLED` master switch
(off by default), explicit ✅/❌ confirm before anything runs; every call logged
server-side regardless of what reaches Telegram.

## Recovery signal foundations

- **HRV is the primary recovery signal** — `hrv_status = BALANCED` means recovered.
- **`DailyMetric.extra`** — everything fetched but not a typed column: sleep DTO (RHR,
  overnight HRV, body-battery change, skin-temp, SpO2, respiration), HRV summary,
  **Training Readiness** (`readiness_score`/`level`, `recovery_time_h`, `acute_load`,
  ACWR `acwr_pct`/`acwr_feedback`), user summary (steps/distance/calories/intensity
  minutes/floors), VO2max, race-time predictions, endurance score. Fed to plan
  generation as a `fitness` snapshot + `weekly_volume` + `recovery` trend.
- **Sync awareness**: `synced_today`/`has_data`/`last_data_date` distinguish "watch
  hasn't synced" from "bad recovery." Morning job runs ~10s after startup then every
  20 min; window (07–12 Europe/Warsaw) + once-a-day guard live in `morning_job`.

## Weather (`app/weather.py`)

`geocode` resolves a typed city once on settings save; `fetch_forecast` (today,
network-safe, `None` on error) feeds the morning report — heat/rain/wind advice only
when a run is today/tomorrow. `fetch_forecast_week` (7 daily rows) + pure
`find_weather_conflicts` (heat/rain/wind/icy thresholds) power EP-13 and ST-13.
`weather` rides in the dedup-cache key.

## Weekly digest (EP-07)

Sunday-evening retrospective (`weekly_digest_job`, `DIGEST_HOUR` Europe/Warsaw).
`run_digest` assembles week vs last-week km/runs/longest, compliance, recovery trend,
fitness snapshot, goal progress **in Python**; Sonnet only narrates (`SYSTEM_DIGEST`,
explicit "відстаєш" below ~70% compliance). No active plan → shortened version.
Dedup-cached on ISO week (not `today`); guarded once/week via `bot_state
digest:<iso-week>`. `/test_digest` calls the same send path (`_deliver_digest`) without
consuming the guard.

## Weather-aware planning (EP-13)

Daily job (`weather_plan_job`) proposes moving/easing a key session (tempo/intervals/
long) landing on an extreme-weather day, within `WEATHER_DECISION_DAYS`. No conflict ⇒
zero Claude calls. `run_weather_plan_check` returns move/modify-only ops
(`_filter_weather_ops`), summary always says "прогноз на зараз" (no auto-apply). Reuses
EP-02's `_send_adapt_proposal`/`adapt_callback`. `_has_pending_proposal` ensures only one
unanswered ✅/❌ proposal at a time across all automatic proposers.

## Sick/travel mode (NF-03)

`/sick [днів]` triggers a *block rebuild*: skip missed/near-term days, ease the return,
re-ramp ~10%/week (`SYSTEM_SICK`, `run_sick_check`). `_filter_sick_ops` allows only
move/modify/skip, dated `today-SICK_LOOKBACK_DAYS..today+SICK_WINDOW_DAYS` (14/14).
Ignores `adjust_level` deliberately (illness overrides the plan's normal bounds).
Non-medical wording; reuses the plan-edit confirm flow.

## Personal records (EP-14)

Pure-Python (`app/records.py`): fastest 5K/10K/half (±5% distance, pace floor
2:30/km), longest distance/duration, biggest ISO-week km, all-time VO2max, best race
predictions. `PersonalRecord` keeps history (each beat inserts a row carrying the
dethroned value). **Backfill-vs-fresh is a date gate**, not a flag — every record
carries its real achieved date, so `announce_worthy` (within `FRESH_DAYS`) naturally
filters out backfilled history. Wired into the morning tick (after matching, before
auto-analysis), `/records`, and fed to report/digest context (+ dedup-cache key).
`backfill-records --email` CLI seeds silently.

## Personal baselines (NF-01)

Pure-Python (`app/baselines.py`): rolling percentiles (p25/p50/p75) over 90 days per
recovery metric (RHR, HRV, sleep score/hours, stress, body battery) →
`{cur, p50, band, n, pos}`. `pos` is neutral low/normal/high; the SYSTEM prompt carries
per-metric valence. `MIN_SAMPLES`=14 gates a metric in. Zero-LLM; feeds `run_analysis`
(report/morning, not `/deep`) and the dedup-cache key.

## Injury-risk radar (NF-04)

Pure-Python (`app/injury.py`): fuses repeated pain (≥2× same body part/14d, from
EP-12 — weighted heaviest), sustained high ACWR (≥140% on ≥3 recent days), rising RPE
at stable pace, and recovery drift (HRV below band + RHR up) → `Assessment(level, score,
signals)`. Calibration gate: `calibrating` until `INJURY_MIN_HISTORY_DAYS` (14).
`run_injury_check` narrates via Sonnet with a deterministic fallback (`injury.summary`)
if the LLM fails. Surfaced via `/risk` and a morning-tick DM, guarded once per
`INJURY_GUARD_DAYS` (5). Feeds NF-09's auto-deload (below).

## Proactive health alerts (EP-08)

Pure-Python (`app/health.py`), recovery/illness sibling of NF-04: reuses NF-01's
percentile bands as thresholds, flags a metric sustained outside its band —
`hrv_low`/`rhr_up`/`sleep_debt`/`stress_high` (3-4 of the last 7 days). Cold-start gate
`HEALTH_MIN_HISTORY_DAYS`=7. `run_health_alert` narrates via Sonnet
(`SYSTEM_HEALTH`) with a deterministic fallback. Surfaced via `/health` and a
morning-tick DM, guarded **per-rule** via `bot_state alert:<kind>`
(`HEALTH_ALERT_COOLDOWN_DAYS`=3). Tick skips the health push when an injury advisory
already went out that day (≤1 risk DM/day). Toggles: process `HEALTH_ALERTS` +
per-user `alerts_enabled`. Feeds the daily report (ST-10, below).

## Health alerts in the daily report (ST-10)

`run_analysis` (report/morning) reuses the same 90-day slice `norm` is built from
(`health.detect`, zero extra cost) and, only when `level="alert"`, folds a compact
`health_alerts` block into context + the dedup-cache key — the prompt aligns tone with
an alert already DM'd separately, doesn't repeat it as a second warning.

## Auto-deload from risk signals (NF-09)

Morning tick tries `_deload_check_for_user` FIRST (before plain injury/health DMs):
when injury is elevated/high or health is actionable, AND a heavy session sits within
`DELOAD_HEAVY_WINDOW_DAYS` (5), calls `run_plan_adaptation(..., trigger="deload",
risk={...})` — a new `SYSTEM_PLAN_ADAPT` rule easing 5-7 days, cutting harder on
`level="high"` or stacked signals. Sends the normal ✅/❌ proposal. Fired deload counts
as that day's risk touchpoint — plain injury/health DMs are skipped when it fires.
`plan_adapt_enabled=False` or `adjust_level="off"` → zero Claude calls.

## Multisport weekly load budget (NF-05)

Pure-Python (`app/multisport.py`): TRIMP-like load per ISO week across **all** activity
types (HR-based Edwards zone weight, per-sport duration fallback when HR is unreliable)
→ `{weeks, this_week, non_run_pct}`. One uniform metric (not Garmin's per-activity
`load`, which would systematically inflate runs). Feeds plan generation, EP-02
adaptation, and the weekly digest (+ its cache key) — not the daily report.

## Forward load forecast (NF-20)

Pure-Python (`app/loadforecast.py`), forward-looking counterpart to ACWR:
`session_load` estimates a planned session's load via `fueling.estimate_minutes` ×
per-type intensity weight; `forecast_week` sums current-week planned+actual load over
the trailing `MIN_CHRONIC_WEEKS`=4 chronic average → forecast ACWR (`ok` <1.4, `warn`
<1.6, else `high`). Calibration gate `MIN_HISTORY_DAYS`=28. Surfaced on `/plan`,
dashboard, and one extra (uncached) line in EP-02 adaptation context.

## Multisport activity analysis (EP-10 phase 1)

`client.fetch_activity_series(activity_id, sport=)` reads different `/details`
descriptor keys per sport — running `{d, p, hr}` unchanged; cycling `{d, spd, pw, hr}`
(speed km/h, power watts when available). `_activity_rows` picks the sport via NF-05's
`multisport.sport_bucket`. `_segments`/`activity_payload` collapse whichever keys are
present (pace+HR vs speed+power+HR); `SYSTEM_ACTIVITY` gained a cycling data section.
Web chart shows speed/power sparklines when present. Swim series deliberately out of
scope (own metric shape). Cache key bumped to `series:v2:<id>`.

## Cycling sessions in the plan (EP-10 phase 3 — cycling only, swimming out of scope)

Opt-in checkbox on `/plan` setup → `intake["cycling"] = {days, avg_min}`, wired into
generation/extension context (not adaptation — that only moves/modifies existing
sessions). `SYSTEM_PLAN` places `type="cycling"` sessions with `kind="ride"` steps
(`hr_zone`, never `pace_min_km`). Three consumers updated: `workout_export.build_workout`
emits Garmin's real cycling `sportType` (previously every push was hardcoded running);
`plan_sync._pushable` already allowed it (not in `_SKIP_TYPES`); `matching` unified
run/cycling distance-based matching into one `_match_distance_based` engine, cycling
identified via `multisport.BIKE_NEEDLES`. Web renders a 🚴 badge; duration estimate
suppressed for cycling (no cycling-pace anchor exists).

## Grade-adjusted pace / GAP (EP-15)

Pure-Python (`app/gap.py`): series gained an `"e"` elevation field (live fetch +
FIT backfill); `smooth_elevation` rolling-means before grade math; `gap_pace_min_km`
rescales a split via the Minetti et al. (2002) energy-cost-of-running polynomial.
`_segments` computes per-segment `gain_m`/`loss_m`/`grade_pct`/`gap_pace` (running
only); `activity_payload` adds whole-activity `elevation_gain_m`/`hilly` (>10 m/km).
EP-01 matching uses GAP for `actual_pace_minkm` on hilly routes. Web detail page gets an
elevation sparkline. Cache key `series:v2:<id>` (elevation-aware).

## Compare-past-self (NF-06)

Pure-Python (`app/compare.py`): `window_pair` picks current N-week window + the same
calendar span N years ago (Feb-29-safe). `repository.window_stats` aggregates both
(run km/count/longest, median pace, HR, HRV/sleep/RHR, VO2max, race predictions).
`run_compare` narrates one honest Sonnet call (`SYSTEM_COMPARE`, flags season/data
differences rather than over-claiming), dedup-cached, `has_signal` bails on thin data.
Surfaced via `/compare [тижнів]` and a monthly auto-block on the first weekly digest of
the month (guarded `bot_state compare:<YYYY-MM>`).

## Quarterly/yearly Wrapped (NF-07)

Pure-Python (`app/wrapped.py`, mirrors `compare.py`): `period_window` (year=52wk /
quarter=13wk rolling). `repository.wrapped_stats` reuses `window_stats` + adds
all-sport breakdown, biggest week, VO2max arc; `records_in_range` pulls milestones.
`run_wrapped` narrates one aesthetic Opus longread (`SYSTEM_WRAPPED`), dedup-cached.
Surfaced via `/wrapped [рік|квартал]`.

## Correlation engine (NF-02)

Pure-Python (`app/correlations.py`): tests fixed lagged metric pairs (sleep→next-day
HRV, stress→HRV, RHR→HRV, …), keeps only statistically defensible ones —
`MIN_SAMPLES`=30, `|r|`≥`R_THRESHOLD`=0.35, Fisher-z 95% CI excludes zero. Nothing
significant → `None`, no Claude call. `run_insights` narrates survivors via one
cautious Sonnet call (`SYSTEM_INSIGHTS`), dedup-cached. Surfaced via `/insights` and a
monthly auto-block on the first weekly digest (`bot_state insights:<YYYY-MM>`).

## Web dashboard (EP-04)

`GET /dashboard` — pure DB read, zero Garmin/Claude, renders <100ms. Reuses `me.
_latest_ring`, `app.charts.trend_series` (extracted from `admin.py`/`me.py` into a
shared module), `plan._dow`/`_dm`, `me._act_meta`. New: `repository.month_cost` (SUM
cost_usd this calendar month). Post-login redirect for non-admins and root `/` both
point here. PWA-minimum manifest/icon, linked only from `dashboard.html`.

## Personal MCP server (NF-08, experiment)

`app/mcp_server.py` — thin read-only stdio MCP wrapper (`pip install -e ".[mcp]"`)
around EP-09's `/ask` tools via the shared `_run_ask_tool` dispatch. Single-user
process (`--email` resolves user_id once). Zero Garmin calls, zero LLM cost on our side.
**Requires Python ≥3.10** — meant to run from a separate venv, not necessarily the Pi;
test skips itself via `importorskip("mcp")`.

## Web chat with the coach (EP-11)

`GET/POST /chat` — same engines as bot `/ask`/`/plan <text>` (`run_ask`,
`run_plan_edit`), not a parallel implementation. `_looks_like_plan_edit` keyword
heuristic picks the engine; a miss falls through to `run_ask` (which can still answer
plan questions via its own tool). `repository.get_chat_history` reads straight off
`ReportLog` — user-scoped not chat-scoped, so Telegram and web share one transcript.

**Shared DB-backed pending-plan-edit state**: the free-text `/plan <text>`/`/sick`
confirm flow moved from Telegram's in-memory `context.user_data` to `bot_state`
(`repository.set/get/pop_pending_plan_edit`) — a proposal shown in the bot can be
confirmed from web chat and vice versa, survives a bot restart. `POST /chat/confirm`
mirrors `plan_callback`. Deliberate v1 scope: **no token streaming** (would need
`AsyncAnthropic`, out of scope for this router); `/sick`'s medical-safe framing stays
bot-only.

## Dialogue about an unconfirmed proposal (ST-23)

While a `/plan <text>`/`/sick` proposal is pending, free text is now a follow-up, not a
restart: `run_plan_edit(..., pending=...)` feeds the unconfirmed proposal + thread so
far into `SYSTEM_PLAN_EDIT`, which splits the reply into a **question** (`answer` filled,
`operations` empty — proposal unchanged) or a **correction** (a COMPLETE new operation
set, never a delta — both proposals are computed against the same unapplied `upcoming`).
Thread capped at `PENDING_THREAD_MAX`=6. Bot: `plan_followup` handler registered last (so
commands still win); `_retire_proposal_message` strips the keyboard off superseded
proposals (single-use pending state). Not covered: automatic proposers' own proposals
(EP-02/EP-13/NF-09) — no follow-ups there yet.

## Per-user timezone (ST-14)

`User.timezone` (IANA, default `Europe/Warsaw`) + `zoneinfo` validation on save.
`app.core.tz.user_tz`/`user_today` are the canonical readers (bot/chat aliases
collapsed into these). `_tick_for_user` computes its own `now`/`today` per user instead
of once for the whole batch — a traveling/non-CET user gets their own morning window.
All once-a-day/week/month `bot_state` guards inherit this via the same `today`.
**v1 scope**: `run_daily`-scheduled jobs' own firing hour stays on process TZ.

## `/costs [YYYY-MM]` (ST-12)

`repository.costs_for_month` — total $, per-`kind` breakdown, cache-hit counts, top-3
priciest calls (cache hits never in top-3). Month boundary computed in the user's OWN
timezone, not UTC.

## Weather chips on `/plan` (ST-13)

`_weather_chips` — best-effort, reuses the exact `find_weather_conflicts` EP-13's job
uses to decide whether to propose a move. Display-only (never calls Claude itself).
Active plan only, not the read-only archived view.

## Collapsed past weeks on `/plan` (ST-22)

`_by_week(today, readonly)` collapses a week once its Sunday is past **and** it doesn't
hold the last completed session (`_last_done_date` — "where I'm coming from" stays
visible). One render path (`<details class="wk">`, expanded = same block + `open`) —
rows stay in the DOM while collapsed, so grep-style tests keep working. Open/closed
state not remembered across reloads (v1 limitation).

## Heat/duration fueling advisor (NF-11)

Pure-Python (`app/fueling.py`): `estimate_minutes` derives session duration from
`steps`/`dist_km`/type; `advise` (today's session only, past
`FUELING_MIN_DURATION_MIN`) returns fluid/carb rates past duration thresholds + an
electrolyte note past `FUELING_HEAT_FEELS_C`. Reuses the SAME weather dict ST-03
already fetched — zero extra calls. Folds into `run_analysis` context + cache key only
when there's something to say.

## Evening sleep-debt nudge (NF-16)

Pure-Python (`app/sleepnudge.py`), fired the night BEFORE a heavy session:
`has_sleep_debt` (NF-01 band, ≥2 of last 3 nights below it, OR Garmin's own
`sleep_need_h` gap ≥`NEED_GAP_H`) AND `tomorrow_is_heavy` — both required.
`sleep_nudge_job` (`SLEEP_NUDGE_HOUR`=21) guarded once/evening. Toggles: `alerts_enabled`
+ process `SLEEP_NUDGE`. **v1 limitation**: no bedtime clock time (nothing stored gives
a wake-time to count back from).

## Activity data management (ST-15/16/17/19/21)

- **Manual resync (ST-15)**: `service.resync_activity`/`resync_days` force-refetch one
  activity or a date range, never touching `subjective`/`analysis`/`step_match`.
  Web: `POST /me/activities/{id}/resync`, `POST /me/resync-days`. Bot: `/resync [date]`.
- **Cache bypass (ST-16)**: `client.fetch_*(force=True)` skips the immutable disk cache.
  **An empty force-refetch never clobbers a previously-good cached copy.**
  `client.cache_del(key)` drops one key.
- **Regenerate analysis (ST-19)**: `run_activity_analysis(force=True)` skips the dedup
  *get* but still writes fresh text to both caches. Web button (1/min guard); bot
  `/activity <id> force`.
- **Hide activity (ST-17)**: `ActivityRecord.is_hidden` excluded from EVERY reader
  (volume/load/records/matching/`/ask`/etc.). `set_activity_hidden` also deletes any
  `PersonalRecord` tied to the activity and un-matches any `PlannedWorkout` it satisfied.
  A resync never resurrects the flag. Web 🙈/👁, bot `/hide <id> [show]`.
- **Manual workout status (ST-21)**: `repository.set_workout_status(done/skipped/
  unlink/link)` + `link_candidates` correct EP-01 mismatches; tags `match_info.
  manual=true` so the auto-matcher leaves it alone thereafter. Web-only `<details>` menu
  on `/plan`.

## Race pack (EP-05)

`app/race.py::GOAL_DISTANCE_KM` maps a race goal to a km target (sibling to `app.goal`'s
prediction-metric map). `run_race_plan` (Opus, `SYSTEM_RACE`) assembles fitness snapshot
+ the plan's already-decided taper sessions + target-date forecast into one narrated
pack (pace, splits, fueling by minute for ≥half, checklist, weather note). Surfaced via
`/race` and a daily auto-trigger `race.TRIGGER_DAYS`=7 before `target_date`, guarded
**per-plan** (`bot_state race_pack_sent:<plan_id>`, so a missed tick can't lose it).
`/plan` shows the last pack as a standing block within `PLAN_BLOCK_DAYS`=14.

## User data export (NF-13)

`GET /me/export` — ZIP of `daily_metrics`/`activities` (JSON+CSV twins),
`personal_records.json`, `plans.json`, `report_logs.json`. Explicit per-model column
allowlists (not `__table__.columns`) — the `users` row is never read by this route at
all, so creds/tokens can't leak by construction. Not a DR mechanism (that's OPS-02).

## Shoe mileage tracker (NF-15)

`app/gear.py` — **not independently live-verified** (no live Garmin account during
build; the community library exposes `get_gear`/`get_gear_stats` but no activity→gear
link endpoint). Mileage comes from Garmin's own per-gear lifetime total (not our own
summed activities — no `gear_id` backfill needed). Every parse is defensive: logs once
and returns "no data" rather than guessing on an unrecognised shape. Refreshed daily via
`plan_sync_job`; `/gear` is then a plain DB read. Warns once past `GEAR_WEAR_KM`=700,
re-warns every `GEAR_REWARN_KM`=150 further (mileage-at-warning stored, not a bare
flag). Retired gear never warns.

## Seasonal multisport intake (NF-12)

`intake["season"] = {sport, sessions_per_week, avg_min}` — a **declared-ahead** accent
(distinct from NF-05's *measured* `multisport`), wired into generation/extension/
adaptation context (uncached — those calls never dedup-cache). No day-of-week binding
(pure weekly budget). `POST /plan/season` reassigns without regeneration.

## Training plans

A user picks a goal + intake on the **web form** (`/plan`); we *prescribe* a dated
program (distinct from Garmin-Calendar `planned_runs`, which we merely read).

- **Generation**: `SYSTEM_PLAN` → JSON validated by `GeneratedPlan`/`PlanWorkout`
  (`_coerce_plan`, one retry, else `AnalystError`). Each workout has a human
  `description` + structured `steps` (recursive warmup/run/recovery/cooldown/repeat,
  `pace_min_km [fast, slow]`) persisted on `PlannedWorkout.steps` and used for the
  Garmin-Connect export. Runs on **Opus** (`MODEL_PLAN_GEN`, `max_tokens=16000`),
  optionally **Fable** via a setup-form toggle (`PLAN_GEN_MODELS` — Fable is 2× Opus
  price, form shows both). `repository.create_plan` archives any prior active plan.
- **Edits**: free-text `/plan <текст>` → `run_plan_edit` (`SYSTEM_PLAN_EDIT` →
  add/move/modify/skip ops) → ✅/❌ confirm → `repository.apply_plan_ops`. **Risky
  edits** get a safer counter-proposal (`alt_summary`/`alt_operations`) and a third 🛡
  button. Both plan-ops calls share the `_complete` helper — prompt-for-JSON + Pydantic
  + one retry, deliberately not SDK tool-use (unlike `/ask`, which needs open-ended
  multi-step lookups a single schema can't express).
- **Recovery-adaptive (EP-02)**: `run_plan_adaptation` runs from a weekly Sunday review
  (`plan_adapt_job`) and a morning nudge (heavy session today + low readiness). Ops
  outside `today..today+window_days` dropped. **Adjust level** (ST-07,
  `intake["adjust_level"]`: off/conservative/flexible, changeable via
  `POST /plan/adjust-level`) bounds how bold adaptation may be —
  `_filter_ops_to_level` enforces it, not just the prompt; `off` skips the Claude call
  entirely. Adapt calls are never dedup-cached.
- **Open-ended goal** (`general`, no target race): `target_date=None`, first block
  `PLAN_BLOCK_WEEKS`=6. Extension is confirm-only — morning nudge within
  `PLAN_EXTEND_LEAD_DAYS`=10 (zero Claude cost), a ✅ tap runs `run_plan_extension`
  (Opus) appending the next block to the SAME plan. A ❌ snoozes
  `PLAN_EXTEND_SNOOZE_DAYS`=3.
- **Strength sessions**: per-weekday picker on setup form — saved Garmin workout
  (cloned via `workout_export.clone_workout`, never mutates the user's template),
  "🆕 інше…" (free-text, generated from scratch via `generate_strength_with_stats`,
  sanitised against Garmin's real category taxonomy), or none. **Preview (ST-05)**:
  "Прев'ю" button POSTs the same generation call, hash-pinned so an edited description
  invalidates its stale preview; a confirmed preview skips the second paid call on
  submit. Chat-based exercise swap (`swap_exercise` op) and from-scratch generation
  (`add` with a `strength` object) both validate category codes against
  `app.garmin.exercises.CATEGORIES` — a hallucinated code never reaches the watch.
- **Strength progression (EP-03)**: a generated (not cloned) session now varies week to
  week — `SYSTEM_STRENGTH_GEN` accepts `weeks>1` and returns one session per week
  (+2.5-5kg or +1 set/+1-2 reps; deload every 4th week, -30-40% volume). Degrades
  gracefully (short/malformed reply → pad/replicate, never fails). Lives entirely in
  per-date `PlannedWorkout` rows, so push/render/chat-edit needed zero changes.
  Clone days (Day 1/Day 2) are never auto-progressed — only from-scratch sessions.

## Health checkups / "Аналізи" tab

Manual logging of periodic medical checkups (blood panels, hormone tests, doctor
visits) — separate from Garmin data. `HealthCheckup`: `date`/`title`/`category`/
`results` (compact JSON `[{name, value, unit, ref_range}]`)/`notes`/`next_due_date`/
`analysis`. `app/db/checkups.py` is user-scoped CRUD + `similar_history`/
`due_for_reminder`. `GET/POST /checkups`, `GET/POST /checkups/{id}`.

- **Interpretation**: `POST /checkups/{id}/analyze` → `run_checkup_analysis`
  (`SYSTEM_CHECKUP`, Sonnet, explicitly user-triggered) feeds the checkup's results +
  up to `CHECKUP_HISTORY_LIMIT`=3 prior same-category checkups. Non-diagnostic: flags
  out-of-range against the *given* ref_range, only ever suggests which specialist type.
  Dedup-cached; `update_checkup` clears a stale `.analysis` on every edit.
- **Reminders**: pure `app/checkup_reminders.py` — `due()` flags within
  `REMINDER_LEAD_DAYS`=7 or overdue; wired into daily `plan_sync_job`, guarded
  **per-checkup, once ever** (editing `next_due_date` on the same row does NOT re-arm
  it in v1 — documented limitation). `/checkups` bot command mirrors `/records`/`/gear`.

## Supplement tracking + lab-monitoring advice

`GET/POST /checkups/supplements` — `Supplement` (`name`/`dosage`/`frequency`/
`started_date`/`notes`/`is_active`). `POST /checkups/supplements/analyze` →
`run_supplement_advice` (`SYSTEM_SUPPLEMENTS`, Sonnet) returns **structured JSON**
(`SupplementAdvice`/`SupplementAdviceItem` — same prompt-for-JSON + Pydantic + retry
recipe as `PlanEdit`) — per-supplement `marker`/`frequency`/`note`, monitoring only
(never dosage/diagnosis advice); `max_tokens` scales with supplement count (fixed a
prior mid-sentence truncation). `parse_supplement_advice` safely re-parses stored text,
falling back to raw prose for pre-format rows. **"Create a checkup template"**:
`POST /checkups/supplements/apply-template` builds one empty result row per distinct
recommended marker into a new `HealthCheckup`, then redirects to its edit page — no new
fill-in UI, just the existing form pre-seeded.

## `/ask <question>` (EP-09) — a bounded tool-use agent

The project's **first SDK tool-use** (deliberate — plan gen/edit/adapt stay
prompt-for-JSON; `/ask` alone needs open-ended multi-step lookups). `run_ask` seeds the
loop with the last `ASK_DEFAULT_N`=3 daily reports + this user's `/ask` exchanges from
the last `ASK_CONTEXT_MIN`=30 minutes (in-context follow-ups answer in one round).
Otherwise `run_ask_agent`: up to `MAX_ASK_ROUNDS`=5 round trips against five read-only,
user-scoped tools — `query_activities`/`query_daily` (capped `ASK_MAX_ROWS`=200,
whitelisted fields), `aggregate_weekly`, `get_activity_detail`, `get_training_plan`. A
tool never raises — errors become `{"error": ...}` the model reacts to.
`MAX_ASK_TOTAL_TOKENS`=60k is a second, cost-based cutoff — hitting either limit
mid-tool-use returns an honest "уточни питання", never a guess. Dedup-cached on question
+ `latest_daily_date`; `ReportLog(kind="ask", tool_rounds=<n>)`. Bot-only (pure-DB
`load_credentials`, no MFA risk).

## Day-over-day continuity & relative day labels

- `run_analysis` (report/morning, not `/deep`) feeds the **previous day's** report via
  `repository.get_last_report` (excludes today — keeps the dedup-cache key stable
  across repeated same-day `/report` presses).
- **`app/daterel.py`** fixes a recurring production bug (a run from позавчора narrated
  as вчора, tomorrow's plan session announced as today's) by precomputing a `day` label
  (`"сьогодні (ср)"`/`"вчора (вт)"`/`"через 5 дн (пн)"` — weekday is a second
  cross-check anchor) on every dated record instead of leaving date arithmetic to the
  model. Applied to `daily[]`/`recent_activities[]`/`planned_runs[]`/`plan_today[]`/
  `records[]`/`previous_report` + a `today`/`today_weekday` anchor;
  `analyze_activity_with_stats` gets `activity_day` **beside**, never inside, `activity`
  (that dict is the cache key — a daily-changing label there would expire every stored
  analysis at midnight). `daterel.annotate` copies, never mutates (payload is shared
  with the dedup cache and PERF-05's 30s memo). `run_analysis` takes `today` from
  `app.core.tz.user_today(user)` — ST-14's per-user "today", not the process's.

## Exercise names & run series

`fetch_exercise_summary` maps Garmin's `name` code to Ukrainian via
`app/garmin/exercise_names.py` at return time (cache stays language-neutral); unknown
codes logged once. `fetch_activity_series` (running) pulls `/details`, resolves
speed/HR/distance by descriptor key, downsamples to ~150 points onto
`ActivityRecord.series`. `/ui`/`/me` render series as pace+HR sparklines with a small
hover handler (progressive enhancement — SVG renders without JS).

## `/activity` analysis

`/activities` lists this user's last 5 (DB read); `/activity <id>` analyzes one.
`run_activity_analysis` builds `activity_payload` (summary + `_segments`), calls Sonnet
(`SYSTEM_ACTIVITY`), stores text on `ActivityRecord.analysis`, logs `ReportLog(kind=
"activity")`. Shares the dedup cache.

## Step-level plan-vs-actual (NF-14)

Pure (`app/stepmatch.py`): `flatten_steps` expands the steps tree into execution order;
`match` pairs it against actual laps (`fetch_activity_splits`, disk-cached) and scores
each *working* step (run/tempo/interval with a pace target) hit/miss with tolerance.
Gated on a session we actually pushed with structure (`garmin_workout_id` + `steps`).
Computed in the morning tick (idempotent, best-effort), stored on `ActivityRecord.
step_match`. Feeds `SYSTEM_ACTIVITY`, a `"🎯 6/8 у цілі"` badge, and a hit-rate summary
into EP-02 adaptation context (systematic misses = pace-target calibration signal).

## Goal progress projection (NF-10)

Pure (`app/goal.py`, mirrors `compare.py`/`wrapped.py`): `weekly_medians` smooths race-
time predictions per ISO week, numpy-free least-squares trend over week *order*,
`project` extrapolates to `target_date` only within `FAR_HORIZON_WEEKS`=12 (honest
refusal beyond that). `verdict` (on_track/close/behind) only appears once a
`target_s` exists — the setup form has no time-target input today, so `/goal` is
trend-only in practice. `goal.summary` is a deterministic formatter, zero Claude calls.
Feeds `/goal` and the weekly digest context.

## Post-run check-in (EP-12)

Stateless inline keyboard after auto-analysis / on `/activity` — RPE 1-10 + optional
body-part pain, callback data carries the activity id (`ci:rpe:<aid>:<n>` etc.), no
`context.user_data`. `repository.set_subjective` merges into `ActivityRecord.
subjective`. `/checkin [rpe] [note]` is the manual fallback. Silence is valid.

**Consumers**: `activity_payload` feeds `subjective` to `SYSTEM_ACTIVITY` (+ cache key).
`app/subjective.py` (pure aggregator, sibling to `injury.py`) shapes `{n, avg_rpe,
rpe_rising, recurring_pain?, recent}` for three prompts: the daily report, EP-02
adaptation, and the weekly digest's plan/fact `overreached` count (easy-intent session
completed but RPE ≥`HARD_RPE`=8 — an under-recovery signal even when objective load
looks fine).
