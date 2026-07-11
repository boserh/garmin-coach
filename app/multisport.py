"""Multisport weekly load budget (NF-05) — a pure-Python, zero-LLM detector.

Our running volume (``weekly_run_volume``) only sees runs, so a 3-hour kite session and
an evening of tennis the day before an intervals workout are invisible to the plan — it
happily stacks a hard run on top of hidden fatigue. Every activity (any sport) already
lives in ``activities``; :func:`weekly_load` turns them into a TRIMP-like load per ISO
week, broken down by sport, so the adaptation and digest calls can reason about the *whole*
training budget, not just the running slice.

Design choice — **one uniform load metric across sports** (HR-based Edwards TRIMP, with a
per-sport duration fallback) rather than Garmin's per-activity ``load``: Garmin only
populates training-load for some sports (runs almost always, kite/tennis rarely), so mixing
it in would systematically inflate runs and defeat the whole point — a fair run-vs-not
comparison. Duration × an intensity multiplier is coarse but comparable across every sport,
which is exactly what a *budget* needs. Kite/tennis HR is often unreliable (watch under a
wetsuit / racket arm), so when HR is missing we fall back to a per-sport duration weight.

No network, no Claude; cheap enough to run on every weekly call. Mirrors the ``records.py``
/ ``baselines.py`` shape: a pure detector fed straight into the Claude context (and, for the
cached digest, into the dedup-cache key — the README pitfall).

Future extension (documented in the ticket): a seasonal accent in the plan intake
(kite-season ⇒ less running volume) — the budget snapshot here is the input that unlocks it.
"""
import datetime as dt
from typing import Iterable, List, Optional

# Assumed max HR for the Edwards zone multiplier. Personal, but a fixed default is fine for a
# *relative* cross-sport budget (we compare sessions to each other, not to an absolute scale).
DEFAULT_HR_MAX = 190

# Coarse sport buckets from Garmin's free-text ``activityType``. Order matters: the first
# keyword found in the (lowercased) type string wins, so "trail_running" → run, "lap_swimming"
# → swim. Everything unmatched (kite, tennis, hiking, strength, …) collapses to "other" — the
# label the LLM reads as "non-run cross-training load".
_BUCKETS = (
    ("run", ("run",)),
    ("bike", ("bik", "cycl", "ride")),
    ("swim", ("swim",)),
    ("strength", ("strength", "training", "gym", "weight")),
)

# Duration-only fallback weight per sport (load per minute) when HR is missing/unreliable.
# Roughly the Edwards multiplier of that sport's typical average intensity: steady endurance
# sports sit ~z2, intermittent sports (tennis) spike higher on average.
_DUR_WEIGHT = {
    "run": 3.0, "bike": 2.0, "swim": 3.0, "strength": 2.0, "other": 2.5,
}


def sport_bucket(type_str: Optional[str]) -> str:
    """Map a Garmin activity type to a coarse budget bucket (run/bike/swim/strength/other)."""
    t = (type_str or "").lower()
    for bucket, needles in _BUCKETS:
        if any(n in t for n in needles):
            return bucket
    return "other"


def _edwards_multiplier(avg_hr: float, hr_max: float) -> float:
    """Edwards' HR-zone weight (1–5) for an average heart rate — a single-zone TRIMP proxy."""
    frac = avg_hr / hr_max if hr_max else 0.0
    if frac < 0.6:
        return 1.0
    if frac < 0.7:
        return 2.0
    if frac < 0.8:
        return 3.0
    if frac < 0.9:
        return 4.0
    return 5.0


def activity_load(
    *, type: Optional[str], dur_min: Optional[float], avg_hr: Optional[float],
    hr_max: float = DEFAULT_HR_MAX,
) -> float:
    """A TRIMP-like load for one activity, uniform across sports. HR present → duration ×
    Edwards zone weight; HR missing/zero → duration × a per-sport fallback weight. No
    duration → 0 (nothing to budget). See the module docstring for why we don't use Garmin's
    per-sport ``load`` here."""
    if not dur_min or dur_min <= 0:
        return 0.0
    if avg_hr and avg_hr > 0:
        return round(dur_min * _edwards_multiplier(float(avg_hr), hr_max), 1)
    return round(dur_min * _DUR_WEIGHT.get(sport_bucket(type), 2.0), 1)


def _iso_week(date_s: Optional[str]) -> Optional[str]:
    if not date_s:
        return None
    try:
        return dt.date.fromisoformat(date_s).strftime("%G-W%V")
    except ValueError:
        return None


def weekly_load(
    activities: Iterable[dict], *, hr_max: float = DEFAULT_HR_MAX,
) -> List[dict]:
    """Per-ISO-week training-load budget from a list of activity dicts (any sport), oldest
    week first. Each activity dict needs ``date``/``type``/``dur_min``/``avg_hr``. Pure and
    side-effect free.

    Each entry: ``{week, load, by_sport: {run, bike, ...}, non_run_pct, sessions, hours}``.
    ``by_sport`` only carries buckets with a non-zero load; ``non_run_pct`` is the share of the
    week's load that is NOT running (the headline "how much hidden cross-training" number)."""
    buckets: dict = {}
    for a in activities:
        load = activity_load(
            type=a.get("type"), dur_min=a.get("dur_min"),
            avg_hr=a.get("avg_hr"), hr_max=hr_max,
        )
        if load <= 0:
            continue
        wk = _iso_week(a.get("date"))
        if wk is None:
            continue
        b = buckets.setdefault(
            wk, {"week": wk, "load": 0.0, "by_sport": {}, "sessions": 0, "hours": 0.0}
        )
        sport = sport_bucket(a.get("type"))
        b["load"] += load
        b["by_sport"][sport] = round(b["by_sport"].get(sport, 0.0) + load, 1)
        b["sessions"] += 1
        b["hours"] += (a.get("dur_min") or 0.0) / 60.0

    out = sorted(buckets.values(), key=lambda x: x["week"])
    for b in out:
        b["load"] = round(b["load"], 1)
        b["hours"] = round(b["hours"], 1)
        run_load = b["by_sport"].get("run", 0.0)
        b["non_run_pct"] = round((b["load"] - run_load) / b["load"] * 100) if b["load"] else 0
    return out


def budget_summary(
    weekly: Optional[List[dict]], this_week: str, prev_week: str,
) -> Optional[dict]:
    """This-week vs last-week load headline from the :func:`weekly_load` buckets (missing
    weeks read as zero). ``None`` when there's no load at all in either week — nothing to say."""
    by_week = {w["week"]: w for w in (weekly or [])}
    cur = by_week.get(this_week)
    prev = by_week.get(prev_week)
    if not cur and not prev:
        return None
    cur = cur or {}
    prev = prev or {}
    cur_load = cur.get("load", 0.0)
    prev_load = prev.get("load", 0.0)
    return {
        "load": cur_load,
        "load_prev": prev_load,
        "delta": round(cur_load - prev_load, 1),
        "non_run_pct": cur.get("non_run_pct", 0),
        "by_sport": cur.get("by_sport", {}),
        "sessions": cur.get("sessions", 0),
        "hours": cur.get("hours", 0.0),
    }
