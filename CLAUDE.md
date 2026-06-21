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

Optional: `GARTH_TOKEN_DIR` (default `~/.garth`), `LOG_FILE` (default `bot.log`), `CLAUDE_CACHE_FILE` (default `claude_cache.json`), `STATE_FILE` (default `state.json`).

## Architecture and data flow

```
Telegram command → bot.py
  → garmin_client.build_payload(days, activity_limit)
      → login() via garth (token cached at ~/.garth)
      → parallel fetch: sleep, HRV, stress, body battery per day
      → activity_summary: activities + exercise sets for strength
      → fetch_planned: calendar items → workout details (pace in m/s → min/km)
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

**Sync awareness**: `synced_today` and `has_data` flags distinguish "watch hasn't synced yet" from "bad recovery." The morning job polls every 20 min between `MORNING_START_HOUR` (7) and `MORNING_DEADLINE_HOUR` (12) and fires once when `synced_today=True`. After the deadline it sends anyway with a warning note.

**Pace storage**: Garmin stores workout step targets in m/s. `fetch_workout_detail` converts to decimal min/km (e.g. 6.58 = 6:35 per km). The system prompt instructs Claude to format these for the user.

**Models**: `/report` and morning job use `claude-sonnet-4-6`; `/deep` uses `claude-opus-4-8`. Cost is logged per call (`~$0.02–0.03` for Sonnet).

**Caching & persisted state**: `analyze()` dedups identical requests via a cache keyed on a hash of the meaningful payload (`daily`, `recent_activities`, `planned_runs`) + today's date + question + model. The volatile `generated` timestamp is deliberately excluded from the key — otherwise it would change every minute and never hit; this is the main gotcha if you touch the key logic. One-week TTL, pruned on save, persisted atomically (tmp + `os.replace`) to `claude_cache.json`. Separately, `bot.py` persists the morning-report-sent date to `state.json` so a mid-morning restart doesn't re-send. Both files are gitignored.

**Security**: `_guard()` in `bot.py` drops all messages from chat IDs other than `TELEGRAM_CHAT_ID`.

## Logging

`logging_setup.setup()` must be called before any module-level loggers are used (done at the top of `bot.py`). Logs go to `bot.log` (rotating, 5 × 1 MB) and stdout. Noisy libraries (httpx, telegram, apscheduler) are suppressed to WARNING.

## TODO

- Wire up `garminconnect` provider (`gconn`) and test `connectapi` + username field.
- Deploy to Raspberry Pi 4 with systemd unit.
