# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

Always use the venv interpreter — the system Python is aliased and will not find the installed packages:

```bash
# Test Garmin data fetch (prints JSON to stdout):
./venv/bin/python garmin_client.py

# Test Claude analysis in isolation:
./venv/bin/python claude_analyst.py

# Start the bot:
./venv/bin/python bot.py
```

Install dependencies:
```bash
./venv/bin/pip install -r requirements.txt
```

## Environment

`.env` must contain:
```
GARMIN_EMAIL=
GARMIN_PASSWORD=
ANTHROPIC_API_KEY=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

Optional: `GARTH_TOKEN_DIR` (default `~/.garth`), `LOG_FILE` (default `bot.log`), `LOG_LEVEL` (default `INFO`; set `DEBUG` to see skip-reason logs), `CLAUDE_CACHE_FILE` (default `claude_cache.json`), `GARMIN_CACHE_FILE` (default `garmin_cache.json`), `STATE_FILE` (default `state.json`).

## Architecture and data flow

```
Telegram command → bot.py
  → garmin_client.build_payload(days, activity_limit)
      → login() via garth (token cached at ~/.garth)
      → per-day fetch: sleep, HRV, stress, body battery (past days served from garmin_cache.json)
      → activity_summary: activities + per-exercise set counts (names mapped via exercise_names.py)
      → fetch_planned: calendar items → workout details (pace m/s → min/km; details disk-cached)
      → compact dict (synced_today, daily[], recent_activities[], planned_runs[])
  → claude_analyst.analyze(payload, question, deep)
      → dedup cache check (hash of payload+date+question+model) — return early on hit
      → Sonnet (/report, morning) or Opus (/deep)
      → AnalystError surfaced as user-visible Telegram message
  → reply_text()
```

The aggregation step in `garmin_client.py` is the critical cost-control layer — raw Garmin responses are never sent to the LLM. Each daily summary collapses an entire API response into ~12 fields.

## Key design decisions

**Garmin auth**: `garth` uses unofficial Garmin Connect endpoints. First run requires interactive MFA; tokens persist at `~/.garth`. The `garminconnect` package (`gconn` provider) is in `requirements.txt` but not yet wired up — migration is pending.

**HRV is the primary recovery signal** because Garmin returns 403 for resting heart rate via garth. `hrv_status = BALANCED` means recovered; any drop or deviation is the main stress indicator.

**Sync awareness**: `synced_today` and `has_data` flags distinguish "watch hasn't synced yet" from "bad recovery." The morning job first runs ~10s after startup (`first=10`), then every 20 min; the window/once-a-day guards live inside `morning_job`, which logs its decision (`MORNING: today synced…`, `MORNING skip: not synced yet…`, etc.). It polls between `MORNING_START_HOUR` (7) and `MORNING_DEADLINE_HOUR` (12, Europe/Warsaw) and fires once when `synced_today=True`; after the deadline it sends anyway with a warning note.

**Pace storage**: Garmin stores workout step targets in m/s. `fetch_workout_detail` converts to decimal min/km (e.g. 6.58 = 6:35 per km). The system prompt instructs Claude to format these for the user.

**Models**: `/report` and morning job use `claude-sonnet-4-6`; `/deep` uses `claude-opus-4-8`. Cost is logged per call (`~$0.02–0.03` for Sonnet).

**Exercise names**: `fetch_exercise_summary` reads Garmin's specific `name` code (e.g. `DUMBBELL_BULGARIAN_SPLIT_SQUAT`), not the coarse `category` — both are present in `exerciseSets`. Names are mapped to readable Ukrainian via `exercise_names.py` (a standalone dict). Output shape is `exercises.sets = {exercise_name: set_count}`. Unknown codes are logged once (`EXERCISE unmapped: <CODE> (add it to exercise_names.py)`) and fall back to a prettified form. The warm-up jog (`category=RUN`) is filtered out.

**Caching & persisted state**: two layers, both on disk and gitignored, both atomic-write (tmp + `os.replace`), both prune expired entries on save and tolerate an empty/corrupt file.
- *Claude dedup* (`claude_cache.json`): `analyze()` keys on a hash of the meaningful payload (`daily`, `recent_activities`, `planned_runs`) + today's date + question + model. The volatile `generated` timestamp is deliberately excluded — otherwise the key changes every minute and never hits; this is the main gotcha if you touch the key logic. One-week TTL. Hit logs `CLAUDE CACHE HIT`.
- *Garmin disk cache* (`garmin_cache.json`): only stable, ID-keyed fetches — past-day `daily:<date>` (30d TTL; today is never cached), `exercise:v2:<id>` (365d; sets are immutable), `workout:<id>` (7d; plans can change). Stores raw codes; exercise-name mapping happens at return. Hit logs `GARMIN CACHE <key>`.
- *Morning state* (`state.json`): `bot.py` persists the morning-report-sent date so a mid-morning restart doesn't re-send.

**Security**: `_guard()` in `bot.py` drops all messages from chat IDs other than `TELEGRAM_CHAT_ID`.

## Logging

`logging_setup.setup()` must be called before any module-level loggers are used (done at the top of `bot.py`). Logs go to `bot.log` (rotating, 5 × 1 MB) and stdout. Root level is `LOG_LEVEL` (default `INFO`); noisy libraries (httpx, telegram, apscheduler) are pinned to WARNING regardless. Run with `LOG_LEVEL=DEBUG ./venv/bin/python bot.py` to see the quiet skip-reason lines (e.g. `MORNING skip: outside window`, `already sent today`).

## TODO

- Wire up `garminconnect` provider (`gconn`) and test `connectapi` + username field.
- Deploy to Raspberry Pi 4 with systemd unit.
