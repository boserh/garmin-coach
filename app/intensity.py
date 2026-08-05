"""NF-24 · intensity distribution: polarization (80/20) and the anaerobic budget.

The biggest analytical blind spot in the app. Until this module, the only intensity signal
anywhere was whole-session ``avg_hr`` — which averages 5×1km at zone 5 together with its jog
recoveries into a meaningless "zone 3". So the single most common amateur mistake, the *grey
zone* (easy runs run too hard, hard runs run too easy), was structurally invisible, and plan
adaptation (EP-02) plus the injury radar (NF-04) steered by volume and ``load`` alone —
without ever seeing the SHAPE of that load.

Everything here is pure: seconds-per-zone in, findings out. The Garmin fetch is
``client.fetch_activity_zones`` and the narration is the existing report/digest prompt, so
this stays trivially unit-testable — the same split as ``injury``/``health``/``baselines``.

Two honest-degradation rules run through the whole module, both learned from NF-01/NF-04:

* **time, not sessions** — a week's distribution is shares of TIME in each zone. Counting
  sessions would let a 20-minute set of strides outvote a 2-hour long run.
* **thin weeks say "not enough data"** — 80/20 is a model for 5+ sessions a week; on two
  runs the shares are noise, and a confident "40% grey zone" off two runs is worse than
  silence because the user would act on it.
"""
import datetime as dt
from typing import List, Optional

# A week needs at least this many zone-carrying activities before its distribution is
# reported at all. Below it, the shares are arithmetic, not information.
MIN_SESSIONS_PER_WEEK = 3

# How many consecutive weeks of grey-zone drift before it's a finding rather than a week.
# One heavy week is training; three in a row is a pattern.
GRAY_ZONE_WEEKS = 3

# Zone grouping. z3 is "grey" by definition here: too hard to recover from, too easy to
# drive adaptation — the zone polarized training exists to avoid living in.
LOW_ZONES = ("z1_s", "z2_s")
GRAY_ZONES = ("z3_s",)
HIGH_ZONES = ("z4_s", "z5_s")

# Session types that make a hard week legitimate — a week the plan itself filled with
# intensity must not produce "your easy runs are too hard" (the coach would be scolding the
# runner for following the coach's own plan).
INTENSITY_TYPES = {"tempo", "intervals", "race"}


def _week_of(date_s: str) -> Optional[str]:
    try:
        return dt.date.fromisoformat(date_s).strftime("%G-W%V")
    except (TypeError, ValueError):
        return None


def _zone_seconds(zones: Optional[dict], keys) -> float:
    if not isinstance(zones, dict):
        return 0.0
    total = 0.0
    for k in keys:
        v = zones.get(k)
        if isinstance(v, (int, float)) and v > 0:
            total += float(v)
    return total


def weekly_distribution(activities: List[dict]) -> List[dict]:
    """Per-ISO-week intensity distribution, oldest first.

    ``activities`` is ``[{date, type, zones, ...}]`` — rows without a usable ``zones`` dict
    are skipped entirely rather than counted as zero, so an old activity or one recorded
    without a HR strap can't drag a week's "easy share" up or down.

    Each entry: ``{week, sessions, total_s, low_s, gray_s, high_s, low_frac, gray_frac,
    high_frac, te_anaer, enough}``. ``enough`` is the honest-degradation flag; ``*_frac`` are
    ``None`` when it's False, so a caller can't accidentally narrate noise."""
    buckets: dict = {}
    for a in activities:
        week = _week_of(a.get("date") or "")
        zones = a.get("zones")
        if week is None or not isinstance(zones, dict):
            continue
        low = _zone_seconds(zones, LOW_ZONES)
        gray = _zone_seconds(zones, GRAY_ZONES)
        high = _zone_seconds(zones, HIGH_ZONES)
        if low + gray + high <= 0:
            continue    # a zones dict carrying only training effect — no time to distribute
        b = buckets.setdefault(week, {
            "week": week, "sessions": 0, "low_s": 0.0, "gray_s": 0.0, "high_s": 0.0,
            "te_anaer": 0.0,
        })
        b["sessions"] += 1
        b["low_s"] += low
        b["gray_s"] += gray
        b["high_s"] += high
        te = zones.get("te_anaer")
        if isinstance(te, (int, float)) and te > 0:
            b["te_anaer"] += float(te)

    out = []
    for b in sorted(buckets.values(), key=lambda x: x["week"]):
        total = b["low_s"] + b["gray_s"] + b["high_s"]
        enough = b["sessions"] >= MIN_SESSIONS_PER_WEEK and total > 0
        out.append({
            **{k: round(v) if k.endswith("_s") else v for k, v in b.items()},
            "te_anaer": round(b["te_anaer"], 1),
            "total_s": round(total),
            "enough": enough,
            "low_frac": round(b["low_s"] / total, 3) if enough else None,
            "gray_frac": round(b["gray_s"] / total, 3) if enough else None,
            "high_frac": round(b["high_s"] / total, 3) if enough else None,
        })
    return out


def detect(
    weeks: List[dict],
    *,
    low_target: float,
    gray_max: float,
    anaerobic_cap: float,
    planned_intensity_weeks: Optional[set] = None,
) -> List[dict]:
    """Turn the weekly distribution into at most a few actionable findings.

    ``planned_intensity_weeks`` are ISO weeks whose PLAN contained a tempo/intervals/race
    session (EP-01 session types). The grey-zone and easy-share findings are suppressed for
    those weeks: a distribution skewed by sessions the plan itself prescribed is compliance,
    not a mistake, and flagging it would have the coach scolding the athlete for following
    the plan.

    Returns ``[{kind, week, detail, ...}]``, most recent first. Empty is the normal case —
    this rides inside the daily report, so it must be silent unless something is off."""
    planned = planned_intensity_weeks or set()
    usable = [w for w in weeks if w["enough"]]
    if not usable:
        return []
    findings: List[dict] = []
    latest = usable[-1]

    # 1. Grey-zone drift — the pattern finding. Needs a run of weeks, not one bad week.
    tail = usable[-GRAY_ZONE_WEEKS:]
    if (len(tail) == GRAY_ZONE_WEEKS
            and all(w["gray_frac"] > gray_max for w in tail)
            and not any(w["week"] in planned for w in tail)):
        avg = sum(w["gray_frac"] for w in tail) / len(tail)
        findings.append({
            "kind": "gray_zone",
            "week": latest["week"],
            "weeks": GRAY_ZONE_WEEKS,
            "gray_frac": round(avg, 3),
            "detail": (f"{GRAY_ZONE_WEEKS} тижні поспіль {round(100 * avg)}% часу — "
                       f"в сірій зоні (поріг {round(100 * gray_max)}%): легкі забігані "
                       f"заважко, важкі — не досить важко"),
        })

    # 2. This week's easy share below target — the plain 80/20 read.
    if latest["low_frac"] < low_target and latest["week"] not in planned:
        findings.append({
            "kind": "low_share",
            "week": latest["week"],
            "low_frac": latest["low_frac"],
            "target": low_target,
            "detail": (f"легкої роботи {round(100 * latest['low_frac'])}% часу проти "
                       f"цільових {round(100 * low_target)}%"),
        })

    # 3. Anaerobic dose over the weekly cap. NOT suppressed for planned-intensity weeks:
    # a plan can prescribe hard sessions, but the total anaerobic dose is a ceiling on what
    # the body absorbs either way — that's the whole point of having a cap.
    if anaerobic_cap > 0 and latest["te_anaer"] > anaerobic_cap:
        findings.append({
            "kind": "anaerobic_over",
            "week": latest["week"],
            "te_anaer": latest["te_anaer"],
            "cap": anaerobic_cap,
            "detail": (f"анаеробна доза за тиждень {latest['te_anaer']:.1f} проти "
                       f"стелі {anaerobic_cap:.1f}"),
        })
    return findings


def summary(weeks: List[dict], findings: List[dict]) -> Optional[str]:
    """One deterministic line for a display surface (``/plan``, the dashboard) — or ``None``
    when there's nothing worth saying. The LLM narrates; this is the fallback that never
    depends on it."""
    usable = [w for w in weeks if w["enough"]]
    if not usable:
        return None
    latest = usable[-1]
    head = (f"⚖️ Інтенсивність за тиждень: легка {round(100 * latest['low_frac'])}% · "
            f"сіра {round(100 * latest['gray_frac'])}% · "
            f"важка {round(100 * latest['high_frac'])}%")
    if not findings:
        return head
    return head + "\n" + "\n".join(f"• {f['detail']}" for f in findings)


def build_context(weeks: List[dict], findings: List[dict], *, zone_thresholds=None) -> dict:
    """The compact context handed to the report/digest/adaptation prompts. Only the last
    few weeks travel — the model needs the trend, not the archive, and every field here is
    input tokens on every call that carries it."""
    usable = [w for w in weeks if w["enough"]]
    if not usable:
        return {}
    return {
        "weeks": [
            {"week": w["week"], "sessions": w["sessions"],
             "low_pct": round(100 * w["low_frac"]), "gray_pct": round(100 * w["gray_frac"]),
             "high_pct": round(100 * w["high_frac"]), "te_anaer": w["te_anaer"]}
            for w in usable[-4:]
        ],
        "findings": findings,
        "zone_thresholds": zone_thresholds or None,
    }
