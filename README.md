# Garmin → Claude Telegram Bot

A personal Telegram bot that pulls health and training data from Garmin Connect, aggregates it into compact daily summaries, sends it to Claude for analysis, and returns a report in Telegram.

The project is designed for personal use. Development and testing are done on macOS, with Raspberry Pi 4 (4 GB RAM) as the target deployment platform.

```text
Telegram
    ↓
garmin_client.py
    ↓
Data aggregation
    ↓
Claude API
    ↓
Telegram report
```

## Features

* Daily recovery analysis
* Sleep, HRV, stress, Body Battery, activities, and workout analysis
* Runna workout plan integration through Garmin Calendar
* Morning automated reports
* On-demand reports via Telegram commands
* Deep analysis mode using a larger Claude model
* Aggressive data aggregation to minimize token usage and API cost
* Response caching to avoid duplicate Claude API calls
* Persistent state so the cache and morning-report status survive restarts

## Project Structure

### `garmin_client.py`

Handles Garmin authentication and data collection.

Data is fetched directly through Garmin Connect API endpoints using `garth.connectapi(...)` rather than garth wrapper classes.

Responsibilities:

* Garmin login
* Data retrieval
* Data aggregation
* Payload preparation for Claude

The aggregation layer is the most important part of the project. Raw Garmin responses are converted into compact daily summaries before being sent to the LLM.

This significantly reduces token usage and cost.

Stable, ID-keyed responses (past-day metrics, strength exercise sets, planned-workout details) are cached on disk in `garmin_cache.json` to avoid refetching on every report.

### `exercise_names.py`

A standalone dictionary mapping Garmin exercise name codes (e.g. `DUMBBELL_BULGARIAN_SPLIT_SQUAT`) to readable Ukrainian names. `garmin_client.py` reads Garmin's specific exercise `name` (not the coarse category) and maps it through this dict; unknown codes are logged so you can add them.

### `claude_analyst.py`

Handles communication with the Claude API.

Features:

* Standard reports using `claude-sonnet-4-6`
* Deep reports using `claude-opus-4-8`
* System prompts for health and training analysis
* Structured error handling through `AnalystError`
* Persistent dedup cache to skip identical requests (see Caching and Persistence)

### `bot.py`

Telegram bot implementation.

Commands:

* `/report` — standard report
* `/deep` — detailed report

Also contains the scheduled morning report job.

The bot checks Garmin synchronization approximately every 20 minutes and only sends the morning report after fresh Garmin data becomes available.

## Installation

Create a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Required packages include:

```text
anthropic
garth
python-telegram-bot[job-queue]
python-dotenv
```

## Configuration

Create a `.env` file:

```env
GARMIN_EMAIL=your_email
GARMIN_PASSWORD=your_password

ANTHROPIC_API_KEY=sk-ant-...

TELEGRAM_BOT_TOKEN=xxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

The application reads configuration from environment variables.

Example:

```python
os.environ["ANTHROPIC_API_KEY"]
```

### Optional environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GARTH_TOKEN_DIR` | `~/.garth` | Garmin token storage |
| `LOG_FILE` | `bot.log` | Log file path |
| `LOG_LEVEL` | `INFO` | Root log level (`DEBUG` shows skip-reason logs) |
| `CLAUDE_CACHE_FILE` | `claude_cache.json` | Claude response cache |
| `GARMIN_CACHE_FILE` | `garmin_cache.json` | Garmin stable-fetch cache |
| `STATE_FILE` | `state.json` | Morning-report status |

## Garmin Authentication

On the first run, Garmin authentication may require MFA verification.

Tokens are stored by garth and reused automatically.

Subsequent runs typically do not require manual login.

## Running

Because macOS may have multiple Python installations and aliases, use the virtual environment interpreter explicitly:

```bash
./venv/bin/python garmin_client.py
```

Verify data collection.

Then start the bot:

```bash
./venv/bin/python bot.py
```

## Garmin Data Sources

Current implementation uses the following Garmin Connect endpoints:

### Sleep

```text
/wellness-service/wellness/dailySleepData/{userName}
```

### HRV

```text
/hrv-service/hrv/{date}
```

### Stress

```text
/wellness-service/wellness/dailyStress/{date}
```

### Body Battery

```text
/wellness-service/wellness/bodyBattery/reports/daily
```

### Activities

```text
/activitylist-service/.../activities
```

### Strength Training Sets

```text
/activity-service/activity/{id}/exerciseSets
```

### Garmin Calendar / Runna

```text
/calendar-service/...
```

### Workout Details

```text
/workout-service/workout/{id}
```

## Important Notes

### Garmin Access Is Unofficial

The project relies on `garth`, which uses unofficial Garmin Connect APIs.

Garmin does not support this approach, and endpoints may change without notice.

### Resting Heart Rate Recovery Metrics

Resting heart rate recovery data is currently unavailable through garth because Garmin returns HTTP 403 responses.

Recovery analysis therefore relies primarily on:

* HRV average
* HRV status
* Sleep quality
* Stress
* Body Battery

### Runna Integration

Runna training plans are synchronized into Garmin Calendar.

The bot retrieves planned workouts from Garmin rather than from Google Calendar.

### Synchronization Awareness

The payload includes synchronization flags such as:

```text
synced_today
has_data
```

This allows Claude to distinguish between:

* Missing Garmin synchronization
* Poor recovery metrics

### Pace Conversion

Garmin workout pace targets are stored in meters per second.

The aggregation layer converts them into minutes per kilometer before analysis.

### Cost

The aggregation layer dramatically reduces token usage.

Typical Sonnet report cost is approximately $0.02–0.03 per report.

Identical requests are served from the local cache, so repeated reports on the same data cost nothing (see Caching and Persistence).

Avoid sending raw Garmin data to the LLM.

### Model State

Claude is stateless.

If long-term comparisons are needed, baselines and historical data should be stored locally and included in the prompt payload.

Potential future improvements:

* Personal baseline tracking
* "Today vs normal" comparisons
* Weekly summaries
* Post-workout analysis

## Caching and Persistence

Three small JSON files persist state across restarts. All use atomic writes, prune expired entries on save, and tolerate an empty/corrupt file (they just start fresh).

### Claude dedup cache (`claude_cache.json`)

To avoid paying for identical Claude requests:

* The cache key is a hash of the meaningful payload (daily metrics, recent activities, planned runs), the current date, the question, and the model. The volatile `generated` timestamp is excluded, so fresh Garmin data invalidates the cache automatically.
* `/report` (Sonnet) and `/deep` (Opus) are cached separately, since the model is part of the key.
* One-week TTL. A hit logs `CLAUDE CACHE HIT`.

### Garmin stable-fetch cache (`garmin_cache.json`)

Only fetches keyed on stable Garmin IDs, to cut request volume:

* `daily:<date>` — past days only (30-day TTL); today is never cached, since its data is still changing.
* `exercise:v2:<id>` — a completed activity's exercise sets (365-day TTL; immutable).
* `workout:<id>` — planned-workout details (7-day TTL; plans can be edited).

A hit logs `GARMIN CACHE <key>`. Raw Garmin codes are stored; exercise names are mapped to Ukrainian at read time, so labels can change without invalidating the cache.

### Morning-report status (`state.json`)

Persists the date the morning report was last sent, so restarting the bot mid-morning does not re-send a report that was already delivered.

Paths can be overridden via `CLAUDE_CACHE_FILE`, `GARMIN_CACHE_FILE`, and `STATE_FILE`.

## Time Zones

Telegram JobQueue uses UTC by default.

To avoid scheduling offsets, configure:

```python
Defaults(
    tzinfo=ZoneInfo("Europe/Warsaw")
)
```

Otherwise scheduled reports may be shifted by two hours depending on daylight saving time.

## Security

The bot responds only to the configured Telegram chat ID.

Garmin credentials remain on the host machine and are never sent to Claude.
