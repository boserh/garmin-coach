"""Proactive health alerts (EP-08) — a pure-Python, zero-LLM recovery-anomaly detector.

The morning report sees "today"; nobody watches the **trend as a trigger**. Every recovery
metric already sits in ``daily_metrics`` and NF-01 already turns a 90-day slice into personal
percentile bands. This detector reuses those bands as the thresholds (the ticket's key idea:
your own band is a far better threshold than a hardcoded number) and flags a metric that has
sat *outside* its band in the unhealthy direction for **several recent days** — HRV below
your norm, resting HR drifting up, sleep systematically short, stress elevated — so a quiet
downward drift earns a heads-up before you notice it yourself.

Sits next to :mod:`app.injury` (NF-04): same "risk signal" chassis, different rules. The
injury radar fuses *load-side* signals (ACWR, repeated pain, RPE/pace divergence) into an
injury-risk score; this is the *recovery/illness* side. The morning tick sends at most one
risk DM (health defers to an injury advisory already sent that day) so the two never
double-ping.

Design guards (false positives kill trust — the EP-08 pitfall):
  * **Personal thresholds** — a day only counts as anomalous when it's outside *your* p25/p75
    band, not a generic cutoff.
  * **Sustained, not a blip** — a rule fires only after ``SUSTAIN_DAYS`` (or ``SLEEP_DAYS`` of
    7) anomalous days in the recent window.
  * **Cold-start gate** — no alerts until ``min_history_days`` of data; and a metric needs
    NF-01's ``MIN_SAMPLES`` days before its band exists, so early history is naturally quiet.
  * **Non-medical** — advice eases training / suggests rest / "see a doctor if it persists",
    never a diagnosis.

Pure and side-effect free: the caller fetches the history, runs :func:`detect`, and decides
what to do (a morning push, the ``/health`` command). The advisory text is optionally narrated
by Sonnet, but :func:`summary` is a deterministic fallback so an alert never depends on an LLM.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app import baselines

RECENT_WINDOW = 7        # look for a sustained anomaly within the last week
SUSTAIN_DAYS = 3         # HRV/RHR/stress out of band this many recent days = sustained
SLEEP_DAYS = 4           # sleep short this many of the last 7 days = a real debt
SEVERE_DAYS = 5          # out of band this many days → bump severity

# Which baseline metrics this detector watches, and the unhealthy direction ("low" = a value
# below the band's p25 is bad, "high" = above p75 is bad). Valence mirrors baselines._METRICS.
# resting_hr/hrv_avg/stress use SUSTAIN_DAYS; sleep uses the of-7 SLEEP_DAYS rule.
_LOW_BAD = ("hrv_avg", "sleep_score", "sleep_h")     # below band is the concern
_HIGH_BAD = ("resting_hr", "stress_avg")             # above band is the concern


@dataclass
class Alert:
    kind: str            # hrv_low / rhr_up / sleep_debt / stress_high
    severity: int
    detail: str          # a human, Ukrainian one-liner (the evidence)
    advice: str          # a non-medical suggestion


@dataclass
class HealthReport:
    level: str                       # calibrating / none / alert
    alerts: List[Alert] = field(default_factory=list)
    history_days: int = 0

    @property
    def actionable(self) -> bool:
        return self.level == "alert" and bool(self.alerts)


def _recent(history: List[dict], key: str, window: int) -> List[float]:
    """The last ``window`` days' non-null values for ``key`` (oldest-first order preserved)."""
    vals = [float(r[key]) for r in history[-window:]
            if isinstance(r.get(key), (int, float))]
    return vals


def _count_below(vals: List[float], low: float) -> int:
    return sum(1 for v in vals if v < low)


def _count_above(vals: List[float], high: float) -> int:
    return sum(1 for v in vals if v > high)


def _has_history(history: List[dict]) -> int:
    """Days carrying at least one recovery scalar (a bare, unsynced row doesn't count)."""
    keys = _LOW_BAD + _HIGH_BAD
    return sum(1 for r in history if any(isinstance(r.get(k), (int, float)) for k in keys))


_HRV_ADVICE = ("HRV нижче твого коридору кілька днів — ознака недовідновлення. Наступні дні "
               "тримай легко, більше сну; якщо додається розбитість/застуда — відпочинь.")
_RHR_ADVICE = ("Пульс спокою вище твоєї норми кілька днів — тіло не встигає відновлюватись "
               "або починається застуда. Полегши навантаження, стеж за самопочуттям.")
_SLEEP_ADVICE = ("Сон системно нижче твоєї норми цього тижня. Недосип б'є по відновленню й "
                 "формі — спробуй лягати раніше; важкі сесії поки обережно.")
_STRESS_ADVICE = ("Середній стрес вище твого коридору кілька днів. Додай відновлення "
                  "(легкий рух, сон, менше кофеїну ввечері), не став важке впритул.")


def detect(
    history: List[dict], *, min_history_days: int = 7, today: Optional[dt.date] = None,
) -> HealthReport:
    """Flag sustained recovery anomalies from a slice of daily rows (oldest-first, as
    ``repository.read_history`` returns them — ideally ~90 days so the personal bands are
    solid). ``min_history_days`` gates the cold start: under it we stay in a quiet
    ``level="calibrating"`` mode and raise nothing (the AC). Pure; ``today`` is accepted for
    symmetry/testing but unused. Returns a :class:`HealthReport`."""
    days = _has_history(history)
    if days < min_history_days:
        return HealthReport(level="calibrating", history_days=days)

    norm = baselines.compute_baselines(history)
    if not norm:
        # Not enough per-metric history for any personal band yet → nothing to compare against.
        return HealthReport(level="none", history_days=days)
    bands: Dict[str, dict] = norm["metrics"]

    alerts: List[Alert] = []

    # --- HRV below your band (undue fatigue / under-recovery) ---
    if "hrv_avg" in bands:
        low = bands["hrv_avg"]["band"][0]
        vals = _recent(history, "hrv_avg", RECENT_WINDOW)
        n = _count_below(vals, low)
        if n >= SUSTAIN_DAYS:
            alerts.append(Alert(
                "hrv_low", 2 if n >= SEVERE_DAYS else 1,
                f"HRV нижче твоєї норми (коридор від {low:g}) {n} з останніх "
                f"{len(vals)} днів.", _HRV_ADVICE))

    # --- Resting HR above your band (fatigue / illness drift up) ---
    if "resting_hr" in bands:
        high = bands["resting_hr"]["band"][1]
        vals = _recent(history, "resting_hr", RECENT_WINDOW)
        n = _count_above(vals, high)
        if n >= SUSTAIN_DAYS:
            alerts.append(Alert(
                "rhr_up", 2 if n >= SEVERE_DAYS else 1,
                f"Пульс спокою вище твоєї норми (коридор до {high:g}) {n} з останніх "
                f"{len(vals)} днів.", _RHR_ADVICE))

    # --- Sleep systematically short (debt) — prefer sleep_score, fall back to hours ---
    for key, label, unit in (("sleep_score", "оцінка сну", ""), ("sleep_h", "сон", "год")):
        if key in bands:
            low = bands[key]["band"][0]
            vals = _recent(history, key, RECENT_WINDOW)
            n = _count_below(vals, low)
            if n >= SLEEP_DAYS:
                alerts.append(Alert(
                    "sleep_debt", 2 if n >= SEVERE_DAYS else 1,
                    f"{label.capitalize()} нижче твоєї норми (коридор від {low:g}{unit}) "
                    f"{n} з останніх {len(vals)} днів.", _SLEEP_ADVICE))
            break  # one sleep alert is enough; don't double-flag score AND hours

    # --- Stress elevated vs your band ---
    if "stress_avg" in bands:
        high = bands["stress_avg"]["band"][1]
        vals = _recent(history, "stress_avg", RECENT_WINDOW)
        n = _count_above(vals, high)
        if n >= SUSTAIN_DAYS:
            alerts.append(Alert(
                "stress_high", 2 if n >= SEVERE_DAYS else 1,
                f"Середній стрес вище твоєї норми (коридор до {high:g}) {n} з останніх "
                f"{len(vals)} днів.", _STRESS_ADVICE))

    alerts.sort(key=lambda a: a.severity, reverse=True)
    level = "alert" if alerts else "none"
    return HealthReport(level=level, alerts=alerts, history_days=days)


# ---------- FORMATTING ----------

def summary(report: HealthReport) -> str:
    """Deterministic advisory for an actionable report — used by ``/health`` and as the
    LLM-free fallback for the morning push. Conservative and non-medical."""
    lines = ["🩺 Сигнали відновлення", ""]
    for a in report.alerts:
        lines.append(f"• {a.detail}")
    lines.append("")
    # One consolidated piece of advice (the highest-severity alert's), to avoid a wall of text.
    lines.append(report.alerts[0].advice)
    lines.append("Це не діагноз — якщо стан не покращується, звернись до лікаря.")
    return "\n".join(lines)


def to_context(report: HealthReport) -> dict:
    """Compact dict of the alerts for the Claude narration context (and the report cache key)."""
    return {
        "level": report.level,
        "alerts": [{"kind": a.kind, "severity": a.severity, "detail": a.detail}
                   for a in report.alerts],
    }
