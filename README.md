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

### `claude_analyst.py`

Handles communication with the Claude API.

Features:

* Standard reports using `claude-sonnet-4-6`
* Deep reports using `claude-opus-4-8`
* System prompts for health and training analysis
* Structured error handling through `AnalystError`

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

Avoid sending raw Garmin data to the LLM.

### Model State

Claude is stateless.

If long-term comparisons are needed, baselines and historical data should be stored locally and included in the prompt payload.

Potential future improvements:

* Personal baseline tracking
* "Today vs normal" comparisons
* Weekly summaries
* Post-workout analysis

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
