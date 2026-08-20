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
restart_services.sh` migrates then reloads `garmin-web` and restarts `garmin-bot` +
`garmin-admin-bot`.

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

**Zero-downtime web reload**: `garmin-web.service` runs `gunicorn "app.main:create_app()"
-k uvicorn.workers.UvicornWorker --workers 2` instead of bare `uvicorn` — the gunicorn
master owns the listen socket for the unit's whole life. `restart_services.sh` sends it
`systemctl reload` (→ `ExecReload=kill -HUP $MAINPID`), which makes gunicorn spawn fresh
workers (re-importing `app.main`, so new code) and only THEN gracefully drain+kill the
old ones — the socket never closes, so `/dashboard`/`/report.json`/etc. never 502 mid-
deploy. `reload` does **not** run `ExecStartPre`, so the script runs `alembic upgrade
head` itself (as `sudo -u pi`, since the script itself runs as root) before reloading.
`garmin-bot`/`garmin-admin-bot` stay plain `systemctl restart --no-block` — they're
single-process Telegram long-pollers with no in-flight HTTP request to protect, so a
few seconds offline is harmless (auto-reconnects). Multiple gunicorn workers meant
`app/db/base.py` now sets `PRAGMA journal_mode=WAL` + `busy_timeout=5000` on every
sqlite connection (was previously journaled/rollback, fine for a single process) so
concurrent workers don't trip "database is locked".

## Recovery signal foundations

- **HRV is the primary recovery signal** — `hrv_status = BALANCED` means recovered.
- **`DailyMetric.extra`** — everything fetched but not a typed column: sleep DTO (RHR,
  overnight HRV, body-battery change, skin-temp, SpO2, respiration), HRV summary,
  **Training Readiness** (`readiness_score`/`level`, `recovery_time_h`, `acute_load`,
  ACWR `acwr_pct`/`acwr_feedback`), user summary (steps/distance/calories/intensity
  minutes/floors), VO2max, race-time predictions, endurance score. Fed to plan
  generation as a `fitness` snapshot + `weekly_volume` + `recovery` trend.
- **`recoveryTime` is MINUTES** (`service.recovery_hours`, migration `d8e9f0a1b2c3`). It was
  stored straight into `extra["recovery_time_h"]`, so the watch's ordinary "19h" arrived in
  every prompt as **1164 годин** — 48 days — and the EP-02 adaptation job proposed a real
  plan rebuild justified by it. That is what makes a unit bug more than cosmetic here: the
  number isn't displayed, it's *read as evidence*. The converter also drops anything past
  `RECOVERY_MAX_H` (168h, above Garmin's own ~4-day ceiling) instead of forwarding a DTO
  shape we no longer understand, `SYSTEM`/`SYSTEM_PLAN_ADAPT` now say the field is hours and
  that a physically impossible value must be ignored rather than acted on, and the migration
  rescales stored history unconditionally (every row came from the one buggy write path, so
  there is no unit ambiguity to guess about).
- **Sync awareness**: `synced_today`/`has_data`/`last_data_date` distinguish "watch
  hasn't synced" from "bad recovery." Morning job runs ~10s after startup then every
  15 min; window (07–12 Europe/Warsaw) + once-a-day guard live in `morning_job`.

## Weather (`app/weather.py`)

`geocode` resolves a typed city once on settings save; `fetch_forecast` (today,
network-safe, `None` on error) feeds the morning report — heat/rain/wind advice only
when a run is today/tomorrow. `fetch_forecast_week` (7 daily rows) + pure
`find_weather_conflicts` (heat/rain/wind/icy thresholds) power EP-13 and ST-13.
`weather` rides in the dedup-cache key.

All three network helpers go through `_get_json`, which retries the *transient* side of
Open-Meteo (timeouts, dropped connections, 408/425/429/5xx) `_RETRIES` times with
exponential backoff — the free service answers 503 for short stretches, and one blip used
to cost a whole day of weather (morning block dropped, EP-13's check silently skipped).
A 4xx that says our request is wrong is never retried; on final failure the helper still
returns `None` and every caller degrades to "no weather", unchanged.

The final warning quotes what the far end actually said (`_error_detail`: `retry-after`/
`server`/`cf-ray` + a body snippet) — Open-Meteo's own `{"error": true, "reason": ...}`
means it is throttling this IP, while an HTML page or a foreign `server:` header means
something in between answered instead. `scripts/weather_probe.py` asks the same two hosts
from the failing box over IPv4 and IPv6 separately (a broken AAAA route on the Pi looks
exactly like a ban until you split them) and prints status/headers/body; zero cost.

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

## dist_km vs steps consistency (`app/plansteps.py`)

A planned session states its volume twice: the headline `PlannedWorkout.dist_km` and the
structured `steps` (which is what `workout_export` turns into the watch workout). Both are
model output, and every proposer (adaptation, weather, `/sick`, chat edits) may legally send
a `modify` with only `dist_km` — `apply_plan_ops` used to write that column alone and leave
the old steps, so an eased long run showed "5.0 км" in the header with a 6000 m step under
it, and the run pushed to Garmin was still the un-eased one. The easing existed on screen
only. `/plan`'s `~NN хв` estimate reads `steps`, so the card contradicted even itself.

`plansteps.reconcile` is the single decision, called from every write (`create_plan`,
`append_workouts`, `apply_plan_ops` add/modify): steps written **together with** a distance
win (they reach the watch, so they define the session and `dist_km` follows their total); a
distance arriving **alone** over pre-existing steps is the coach's intent, so the stale steps
are re-cut to it. Re-cutting puts the change on the work steps (`run`/`ride`) and leaves the
warmup/cooldown as prescribed — a coach cutting volume cuts the work — falling back to
proportional scaling only when the fixed parts alone already exceed the target. `repeat`
blocks keep their `reps`; only distances move. A session whose steps are purely time-based
has nothing to reconcile (`total_dist_m` → `None`, never 0).

Every mismatch logs a WARNING (`PLAN dist/steps mismatch`): the prompts demand agreement at
the source, so one appearing here means a prompt regressed, not merely a row to patch. Rows
written before this existed are repaired by `python -m app.cli fix-plan-steps --email … [--apply]`
(0 Garmin, 0 LLM, dry-run by default); it also names the sessions already on the calendar,
which still carry the old workout until an `unpush-plan` + `push-plan`.

## Sick/travel mode (NF-03)

`/sick [днів]` triggers a *block rebuild*: skip missed/near-term days, ease the return,
re-ramp ~10%/week (`SYSTEM_SICK`, `run_sick_check`). `_filter_sick_ops` allows only
move/modify/skip, dated `today-SICK_LOOKBACK_DAYS..today+SICK_WINDOW_DAYS` (14/14).
Ignores `adjust_level` deliberately (illness overrides the plan's normal bounds).
Non-medical wording; reuses the plan-edit confirm flow.

## Declared away periods (NF-34)

**The bug was a divergence, not a missing feature.** The daily report *appeared* to know
about a vacation ("вже після відпустки") because it reads yesterday's report and the coach
memory, so one mention leaked forward a day at a time. The Sunday digest read neither, and
scored the same deliberately empty week as *«compliance 0%… ні, відстаєш»*. In the data a
planned week off and a collapsed week are the same zero — only the athlete knows which, so
the athlete has to be able to say it once, somewhere every surface reads.

`AwayPeriod` (`away_periods`: start/end/kind/note) + pure rules in `app/away.py` +
storage/context in `app/db/away.py`. `kind` is closed and tiny — `rest`/`active`/`sport`/
`work` — with a per-kind `expect` line, because the *load* is the whole point: a beach week
detrains, a trekking week is quiet volume on tired legs, a kite week is daily fatigue with
zero running. The athlete's own words live in `note`.

- **One context helper, like the profile.** `away_db.build_context` is called by the daily
  report, the digest, `/ask`, plan generation, adaptation, `/sick`, the injury radar, the
  health alert and weather planning; `AWAY_BLOCK` is appended to all of those prompts in one
  place (`prompts.py`). The advisories were the second half of the same bug: the injury
  radar fired a perfectly correct "HRV below baseline three days" during a declared kite
  week and then advised *«прибери tempo/intervals/long, можу перебудувати план»* — a real
  signal with an action the athlete cannot take, since nothing is scheduled. Hence the
  block's rule that while a period is `active` the advice must be about what the athlete can
  actually change there (sleep, the load of what they're doing, an easier day) and any
  mention of the plan belongs to the RETURN. Two surfaces knowing
  different things about the same week is the defect, so there is exactly one wording and
  one reader. `None` (not an empty dict) for someone who never declared a period — their
  prompts stay byte-for-byte pre-NF-34.
- **`days_in_week` is computed in Python**, from the ISO week the digest is judging — the
  same rule as the relative day labels: never leave date arithmetic to the model.
- **It is context, not an indulgence.** The block says so explicitly: missed *runs* inside
  the period aren't a compliance failure, but visible load (kiting, hiking) and a sunk HRV
  are still reported as usual — and catching up the missed volume on return is called out
  as the injury it is.
- **Three doors, one validator.** `/away 16.08-24.08 кайт` (pure parsing, 0 Claude), the
  `/me/profile` form, and — how it will actually happen most of the time — a `/plan` edit
  ("зсунь тренування, я у відпустці"). The edit prompt returns an extra `away` field
  (`AwayOp`), which rides *with* the pending proposal and is written on the same ✅
  (`away_db.apply_pending`, shared by the bot's `plan_callback` and `/chat/confirm`): one
  request is one decision, so ❌ leaves no trace and ✅ records the trip even when there are
  no plan operations at all. An LLM-proposed period goes through the same
  `away.normalize` bounds as a typed one, and `from_op` swallows junk rather than sinking
  the edit it rode along with.
- **Bounded on purpose** (`MAX_DAYS` 120): an open-ended "away" would silence the coach's
  compliance judgement forever, which would be a worse bug than the one being fixed.
- **The nudges stay quiet**: `_sickness_check_for_user` returns before the detector runs
  while a period is active — three missed sessions during a kite week is not an illness to
  repair, and that DM is exactly the nag this feature exists to stop.
- Part of the dedup-cache keys (`_cache_key`, `_ask_cache_key`, `_DIGEST_KEY_FIELDS`):
  declaring a trip has to produce a fresh report, not a hit on the one written before the
  coach knew.

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

**Two windows** — the 90-day median is a *stable reference*, and that is the point for the
threshold readers (`health.detect`, `sleepnudge`, the dashboard ring, all still on
`p50`/`band`), but it is also why the morning report quoted the identical "медіана 51 /
коридор 44–49" every day for weeks: with ~90 integer samples clustered on three or four
values, one day in and one day out cannot move the middle rank. Arithmetic, not a bug — but
useless in a daily narration. So each metric also carries `recent` (`{p50, band, n, days}`
over `RECENT_DAYS`=28, gated by its own `RECENT_MIN_SAMPLES`=10), `pos_recent`, and `trend`
(signed `recent.p50 - p50`, omitted when it rounds to zero). The prompt narrates `recent`
as "your norm now", uses `trend` for drift, and is told **not** to recite baseline numbers
that didn't change. The recent slice is cut **by date**, not by position — `read_history`
returns one row per *stored* day, so a positional tail reaches months back across a sync
gap (positional only as a fallback for undated rows). `cur` also carries `stale_days` when
the most recent non-null predates the newest day in the slice (Garmin fills some metrics
late), so a two-day-old HRV is no longer narrated as this morning's.

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

## Auto sickness trigger (NF-18)

The `/sick` rebuild offered without `/sick` — an actually ill user never types it.
`_sickness_check_for_user` runs right AFTER the deload check and only when that stayed
silent (they share `INJURY_WARNED_KEY`: never two risk DMs a day). Fires on **both**
conditions: a streak of ≥`SICKNESS_MISSED_DAYS` (3) consecutive `missed` plan sessions in
the last 7 days (`app.sickness.missed_streak`, pure) **and** an actionable EP-08 health
report — missed sessions alone are as likely to be a business trip. Streak walks backwards
from the most recent past session: `done`/`partial`/`skipped` break it, `planned` rows
(rest/cross, which the matcher never touches) are invisible so a rest day can't reset it;
today's session is excluded (it may still sync). Deliberately NOT NF-09's signal —
NF-09 looks forward at heavy sessions and eases the future, this looks back at a broken
past and repairs it (a user who missed half the week may have nothing heavy ahead).
The DM is a plain ✅/❌ question, **zero Claude calls**; `sickness_callback` runs the same
`run_sick_check` as `/sick` (`days_missed` = the streak) only on ✅, then hands the result
to the normal plan-edit confirm flow. ❌/ignored → `SICKNESS_WARNED_KEY` snoozes for
`SICKNESS_GUARD_DAYS` (7). Yields to any pending proposal (`PENDING_ADAPT_KEY` or a
pending plan edit); gates: `SICKNESS_AUTO` + `alerts_enabled` + `plan_adapt_enabled`.

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
scope (own metric shape). Cache key bumped to `series:v2:<id>` (now `v3` — see NF-25/NF-33).

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
elevation sparkline. Cache key `series:v2:<id>` (elevation-aware; now `v3` — see NF-25/NF-33).

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
`has_sleep_debt` (NF-01 band on `sleep_h` OR `sleep_score`, ≥2 of last 3 nights below it,
OR Garmin's own `sleep_need_h` gap ≥`NEED_GAP_H`) AND `tomorrow_is_heavy` — both required.
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

**`get_training_plan` returns session CONTENT, not just its one-line description.** A
strength day's `description` is the template's name ("Day 1") and its `dist_km` is null,
so a plan row on its own says nothing about the session — the tool answered "there are no
details" for a day whose exercises were sitting in the DB all along (same shape as ST-09's
hole in the morning report, one path over). Each session now carries a `detail` ref into a
top-level `session_details` map: `{"steps": …}` for a run, `{name?, blocks:[{sets?,
rest_s?, exercises:[{name, category?, exercise?, reps?, weight_kg?}]}]}` for a strength
day, read **from the DB only** (`strength_plan` → `strength_snapshot`, incl. the legacy
flat-list shape) — no live template fetch on a read-only tool path. Exercises carry the
human `label()` *and* the Garmin codes, which is what an activity's logged sets are keyed
by, so plan-vs-actual lines up. The map is shared rather than inlined because a plan
repeats the same session weekly: inlining Day 1's exercises on every date is what would
eat `MAX_ASK_TOTAL_TOKENS`. No `detail` key = genuinely nothing stored, and `SYSTEM_ASK_
TOOLS` says to report that instead of reconstructing exercises from `query_activities`
(the exact hallucination ST-09 documented).

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
- **A named output slot outranks the reading rule.** The labels were all in the prompt
  and the report still narrated позавчора's windsurf session as "вчора" — because the
  `ФОРМАТ ВІДПОВІДІ` spec named that paragraph *«Вчорашнє навантаження»*. On a day whose
  yesterday held no activity, the model filled the slot it was told to write with the
  newest session it had, and inherited the slot's word. Output-shape lines must stay
  relative-word-free (the slot is «Нещодавнє навантаження» now, and says explicitly to
  report an empty yesterday as empty); `tests/test_daterel.py` asserts the wording.
  `daily[].extra.auto_activities` has the same trap one level down — it's nested inside a
  daily row and has no `day` of its own, so the prompt spells out that its day is the
  **parent** row's label, not that of a neighbouring `recent_activities[]` entry.

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

## Post-race debrief (NF-23)

Pure (`app/postrace.py`): `build_debrief` turns a race into numbers before any narration
exists — the per-kilometre curve, `split_halves`, `fade_point`, `decoupling_pct` and
`target_comparison`. Everything is judged on **GAP** pace (`app.gap`); raw splits on a real
course describe the hills, so a fade found on raw pace would be terrain, not the runner
(there is a test with a flat-then-climb profile for exactly this).

Three input tiers, deliberately: laps (`fetch_activity_splits`, disk-cached — the runner's
own splits) → the per-point `series` when auto-lap was off → aggregates only. The last tier
still narrates; nothing here may raise on missing data.

The target pace comes from the structured `TrainingPlan.intake.target_time_s` (NF-17), never
from parsing the narrated pack — the ticket's own brittleness note. No target → the whole
`target` block is absent rather than zero-filled.

One Sonnet call (`SYSTEM_RACE_DEBRIEF`, `run_race_debrief`), prompt focused on **three
takeaways for the next cycle** rather than a retelling of the numbers. `activity_id` is in
the dedup key, so a repeated `/race done <id>` is a cache hit.

Delivery is a T+1 race-week stage (`race.stage_for` returns `"debrief"` for negative
`days_left` within `DEBRIEF_CATCHUP_DAYS`=3 — the watch may sync late). The per-(plan, stage)
guard key moved into `race.stage_guard_key` so `repository.archive_plan` can cancel an unsent
debrief by pre-setting it. Race-day weather is stashed by the T-1 stage
(`race.WEATHER_STATE_PREFIX`), because after the race the forecast endpoint no longer covers
that date. The race activity is found only by explicit evidence (`type="race"` or
`target_date`) — never a "fast and long" heuristic.

## Running dynamics (NF-25)

`series:v3` adds `cad`/`gct`/`vo` from the same `/details` response (zero new Garmin
requests). `app/rundynamics.py::session_dynamics` measures within-session drift on **flat
points only** — a climb legitimately shortens the stride, so an unfiltered number would
describe the route; with no elevation channel it reports `flat_filtered=false` instead of
pretending. Gated at 30 minutes (a shorter session has no tired last third).

`build_trend` compares weeks using **easy runs only** (`efficiency._easy_corridor`), since
cadence on intervals is structurally different. `drift_streak` feeds
`injury._dynamics_signal` at severity 2 — a contributing signal, never a warning on its own,
same weighting rule as NF-24's grey zone.

The consumers report **fact and change only**. `SYSTEM_ACTIVITY` is explicit that no
technique prescriptions are allowed, and there is a test for the tone.

## Same-route recognition (NF-33)

`series:v3` also carries `lat`/`lon` (one key bump for both this and NF-25). `app/routes.py`
stores a **fingerprint, never a track**: a start point coarsened to three decimals (≈110 m —
a block, not a doorstep), distance, climb, a normalised elevation profile and a bearing
sequence, under a kilobyte and unreconstructable.

`similar()` needs start proximity + distance + agreeing shape; the bearing sequence is what
makes the same loop run **backwards** a different route (deliberate — comparing them would be
dishonest), and it keeps working on flat routes where the elevation profile says nothing.
`match()` takes the FIRST similar cluster, which is what makes `backfill-routes` idempotent.

Assignment happens inside `persist_payload`, so a run is recognised as it lands. The privacy
rule is structural: `build_route_context` emits an anonymised `route_id` plus pace/HR deltas,
and a test asserts no coordinate reaches the assembled LLM context (a leak here would put a
home address into `report_logs`).

## Return-to-run protocol (NF-30)

Pure (`app/returntorun.py`), **zero LLM by construction** — the ladder is deterministic and
the only paid call in the feature is an optional plan rebuild on the way out.

The progression rule is the runner's own pain number (EP-12's scale): ≤2/10 during the
session AND the next morning → step up; above → repeat the rung; rising on two consecutive
steps → stop and point at a professional. A day with no run is `"idle"` — it moves the step
neither way, because silence is not evidence.

The plan is `status="paused"`, not archived (`repository.CURRENT_PLAN_STATUSES`), so `/plan`
still shows it with a banner and its future sessions survive. Protocol sessions are ordinary
`PlannedWorkout` rows with structured steps, so the Garmin push and the EP-01 matcher work
unchanged — walk/run needed no new DTO.

Entry is always an offer (morning tick with a `RETURN_GUARD_DAYS` guard, or `/pain`), and a
declined offer touches nothing. The medical boundary is enforced by a test over every string
the feature can emit: no diagnosis, no injury name, no "push through it" — plus a
`RETURN_TO_RUN` master switch.

## Coach memory: weekly accumulation (EP-18 phase 2)

One Sonnet call a week inside `weekly_digest_job` (`run_profile_update`) proposing a
**delta** — `{add, confirm, contradict, drop}` — against the stored facts, never a rewrite,
so a bad week cannot erase a year.

The anti-poisoning rules are code, not prompt etiquette: every proposed fact must cite a
`report_logs` id (`repository.reports_for_evidence`) or `profile.normalize_fact` drops it; the
stop-list refuses a statement the user rejected even when it is proposed again; `contradict`
lowers confidence instead of deleting; `parse_profile_delta` caps additions at three a week
and returns an empty delta on anything unparseable (never half-applied). A failure is
swallowed by the job — the digest must not depend on it, and yesterday's profile is a
perfectly good profile.

## Web UI conventions (UI batch, 2026-08)

The batch answered one question — *what is already computed or stored that the user can't
see, or can't reach from a phone?* — and the mechanisms it left behind are the ones to
respect when touching the web layer.

**One `<head>`, one asset version (UI-02).** Every page `extends "_base.html"`;
`app.templating.create_templates()` is the only Jinja environment, and `asset_v` is a
digest of `app.css`/`app.js`/`sw.js` bytes. The manual `?v=` bump it replaces was not
theoretical: it had drifted from `?v=3` to `?v=4` while the mobile-layout guard still
substituted the old literal, so the guard was measuring an **unstyled** page and passing
on nothing. That's why the substitution now asserts it happened. Inter is self-hosted
(one variable woff2 per subset, refreshed by `scripts/fetch_inter.py`): the app runs on a
Pi on the LAN, so a CDN font meant the first paint waited on the public internet and
offline rendering silently changed the metrics the guard measures.

**One chart tooltip (UI-01).** `app.js` binds `.chart[data-pts]`; the SVG itself stays
server-rendered by `app/charts.py`. Pointer Events cover mouse, finger and stylus in one
path — the four inline copies it replaced were all `mousemove`-only, so on the device the
app is designed around the charts were decoration. Two non-obvious rules: the finger gets
`setPointerCapture` so a drag survives leaving the card, and the bubble does **not** hide
on `pointerup` — a phone has no `mouseleave`, and a tooltip that vanishes on release reads
as "nothing happened". `touch-action: pan-y` goes on `.cwrap`, never on `body`.

**Notices are data (UI-07).** A router builds `[{level, icon, text, link, link_text}]`
(`app.banners.banner`) and `_banners.html` renders it; the level picks both the colour
(`color-mix` over the existing tokens) and the ARIA role — `alert` for warn/danger,
`status` otherwise. An unknown level raises rather than producing an unstyled, role-less
div. The navigation is one section list rendered twice (horizontal row on desktop, fixed
bottom tab bar under 36rem); "Ще" is a `<details>` because the site works without JS.

**Pages display, modules compute (UI-05, UI-06).** `/insights` and `/strength` re-derive
nothing and hardcode no threshold — cold-start copy pulls its numbers from
`INJURY_MIN_HISTORY_DAYS`, `loadforecast.MIN_HISTORY_DAYS`, `correlations.MIN_SAMPLES`.
Both are **0 Claude calls / 0 Garmin requests**, and the tests enforce it by replacing the
Anthropic client and the Garmin provider with functions that raise, plus asserting no
`report_logs` row appears. "Let Claude phrase it nicer" is a separate, paid path
(`run_insights`) and deliberately not wired in. Calibrating is rendered as calibrating: a
quiet radar on a short history is not a green light.

**Additive JSON, never a migration (UI-08).** `stepmatch.match` gained a `steps` list
while `steps_hit`/`steps_total`/`misses` kept their exact meaning and remained the source
of truth for the counters. A row stored before the change simply has no `steps` key and
renders as the badge alone. The per-step deviation is measured from the **nearest edge**
of the target range, not its midpoint — a lap on the fast edge missed by nothing — and the
pace-curve shading is placed by **distance**, because the curve's x axis is sampled by
distance and a band placed by lap index drifts off the line it marks.

**Class names are global.** UI-08's row class started as `.step`, which was already the
plan's structured-step line; `/plan` went into horizontal scroll until it became `.sbrow`.
The mobile guard caught it — grep before naming.

## Offline & install (UI-03)

The manifest shipped with EP-04 but there was no service worker at all, so Chrome offered
a bookmark rather than an install, and offline was a white page — on a server that lives
in the house and therefore disappears whenever you leave it.

`app/static/sw.js` is deliberately tiny and dependency-free (a broken worker is sticky).
Three strategies — cache-first for `/static/*`, **network-first** for `GET /dashboard`
and `GET /plan`, network-only for everything else — and a **deny-list that overrides all
three**: `/login`, `/register`, `/logout`, `/settings`, `/onboarding`, `/admin/*`,
`/me/export`, `/status`
and any non-GET are never cached, are evicted if an older worker ever stored them, and
`POST /logout` wipes every cache (the page also messages the worker on submit, because a
navigation can outrun a `postMessage`; the shell then legitimately re-warms — CSS and fonts
are not anyone's data).

**Why network-first and not the stale-while-revalidate the ticket asked for.** SWR serves
the saved copy whenever one exists, so the honest "дані станом на HH:MM" banner appeared on
every ordinary ONLINE visit and claimed there was no connection when there plainly was. The
two ways out contradict each other: hide the banner and a stale readiness number silently
passes for today's, or keep it and the page cries wolf every time. Network-first dissolves
the contradiction — online you get the real page and no banner at all, and the saved copy
(with the banner) appears only when the server genuinely didn't answer, which is exactly
what the banner claims. The cost is one LAN round-trip on a page that was already a DB
read, which is the right trade for an app whose subject is a number that changes daily. A
3-second timeout covers the "phone has WiFi but no route home" case, where a bare `fetch`
would hang instead of failing; if that request lands late, the worker tells the page so it
can offer the fresh copy.

Two more details worth keeping:

- **The cached page admits it is cached.** `_base.html` always emits an
  `<!--sw-offline-slot-->` marker; the worker replaces it with an "дані станом на HH:MM"
  banner (timestamp from a `sw-cached-at` header it stamps on store) only when serving from
  the cache. In an app about readiness, a stale number passing for a fresh one is worse
  than no number.
- **Scope.** A worker's default scope is its own directory, so one served from `/static/`
  could only ever control `/static/`. `app.main._RevalidatingStatic` sends
  `Service-Worker-Allowed: /` for `sw.js` and the registration asks for `{scope: '/'}` —
  the alternative was a bespoke route just to move one file to the root.

The version comes from the same `?v=` digest as the other assets, so a deploy changes the
worker's script URL: `skipWaiting` + `clients.claim` + dropping caches whose name doesn't
carry the current version means a changed `app.css` lands without "clear site data".
Install criteria needed raster icons (`scripts/render_icons.py` renders 192/512 plus a
`maskable` variant from the SVG — Android crops to its own shape and would otherwise clip
the logo).

`tests/test_pwa_offline.py` starts a real uvicorn on a loopback port: a service worker will
not register over `file://`, and `http://127.0.0.1` is a secure context.


## Registration → setup flow

The failure was never a crash. Registration ended on the login page with one green
sentence, the first login dropped the user into `/settings` — eleven fields, no statement
of which three matter — and nothing anywhere said the Telegram bot had to be connected at
all. People filled in Garmin, saw a silent app, and assumed it was broken.

Three pieces:

- **`app/onboarding.py`** — the checklist as data, pure (flags in, step dicts out), the
  same shape as `app.banners`. Garmin → Claude key → Telegram are `required`; the plan is
  the payoff step and is deliberately excluded from `progress()`, so a configured account
  never reads as unfinished. A Garmin password Garmin has since **rejected** counts as
  *not done* (`garmin_creds_invalid`): credentials are stored, the sync is stopped, and a
  tick there would be a lie in the one state that needs action. `User.setup_complete`
  mirrors the same three, and everything else reads it — the post-login redirect, the nav
  entry (present only while unfinished), the `/dashboard` banner.
- **`app/routers/onboarding.py` + `onboarding.html`** — `GET /onboarding`, a pure DB read
  (0 Claude, 0 Garmin, guarded by a test): one card per step, live status, the action next
  to it. Classes are `.ob*` — `.step`/`.steps` are already the plan's structured-step line
  and its container, and that exact collision pushed `/plan` into horizontal scroll once
  already.
- **`app/core/tglink.py`** — Telegram linking as a signed deep link instead of a copied
  chat id. The old flow was: find `@userinfobot`, message it, copy a number, paste it into
  a form, and get no feedback if you got it wrong. Now the web renders
  `t.me/<bot>?start=<token>`; Telegram hands the token back as `/start <token>`, so the
  chat id and the web account arrive in the same update and the bot links them itself.
  The token is a signed blob (user id + issued-at + a truncated HMAC, 24h TTL, its own
  salt) — no table, no migration, no cleanup. It is signed by hand rather than with
  `itsdangerous` because Telegram allows a `?start=` payload of at most 64 characters
  from `A-Z a-z 0-9 _ -`, and `URLSafeTimedSerializer` joins its parts with **dots**
  (`MQ.anZMaQ._Epkc…`) — url-safe in general, outside Telegram's set here, so what a
  client did with the payload was anyone's guess and the button silently linked nothing.
  Moving the serializer's separator doesn't help either: it un-signs by splitting from
  the right, and its own base64url alphabet already contains both remaining candidates.
  Fixed-width fields + one HMAC = exactly 32 base64url characters, no separator at all.
  It does require the bot and web processes to share
  `APP_SECRET_KEY` (they already do, same `.env`); without it `tglink.available()` is
  False and both pages fall back to the manual chat-id field, rather than offering a
  button that cannot work.

`bot/handlers.start_cmd` is the one command deliberately **not** wrapped in
`@bot_command`: its whole job is to run for a chat `_resolve_user` would reject, and the
signed token is what authorises it. `telegram_chat_id` is UNIQUE, so re-linking hands the
chat over (clearing the previous owner) instead of hitting the constraint — both halves
are proved, the token for the web account and the incoming update for the chat. A bare
`/start` explains the actual order of operations; every linking reply ends with what the
account still owes (`onboarding.missing_labels`), because sending someone away with
"готово" when there's no Claude key yet just moves the confusion one step on.

## Admin impersonation ("Переглянути як")

Support answers to "чому в мене порожньо?" were being reconstructed out of `/ui` row by
row, which shows the rows and not the page — and the page is where the answer usually is
(a banner nobody read, an empty state, a plan that ended). So an admin can borrow a
user's session from `/admin/users` instead.

The session carries **both** ids: `user_id` becomes the target (so every existing
`current_user` route stays user-scoped with no per-router change at all) and
`impersonator_id` holds the admin. That second key is the feature — the banner, the stop
route and all three guards key off its presence, so there is no second flag to keep in
sync with it.

Borrowed ≠ acting. `app/core/impersonate.py` bounds it three ways, each at a choke point
rather than in the routers:

- **Read-only.** `current_user` refuses any non-GET (`ImpersonationReadOnly` → 403). It's
  in the dependency every authenticated route already declares, so a router written next
  year inherits the rule. `POST /impersonate/stop` reads the session directly and is the
  one write that still works.
- **Spends nothing.** Same kill-switch shape as the demo account (`app.core.demo`): a
  `IMPERSONATING` ContextVar set for the request, checked in `user_runtime` and
  `analysis.client._get_client`. Support looking at an account must not bill the user's
  Claude key or burn their Garmin rate limit — which is also why `/status` names the skip
  ("impersonation — Garmin not contacted") instead of letting the guard's exception land
  in its catch-all and read like a broken Garmin login.
- **No admin.** `require_admin` refuses while impersonating, and an admin can't be
  impersonated at all. Admin pages span everyone's data; "who did this" has to stay
  answerable.

The bar lives in `_base.html`, not in a router's `banners` list: it's a fact about whose
session this is, not about the page, and the one state where a template that forgot the
notice means someone reads another person's data without seeing whose. It reads the
emails straight out of the session (cached there at start, so no query per page) and is
styled in `--race`, a hue the chrome uses nowhere else. Start and stop log at WARNING —
the session is read-only and costs nothing, but somebody still looked.

Loose ends handled: `login_session` clears the impersonation keys, so signing in again
after closing the tab mid-session gives a clean session rather than a borrowed one with
a new id pasted over it; and if the admin is demoted, deactivated or deleted while
looking, stopping signs out entirely instead of handing back rights that no longer exist.
