"""NF-34 · away periods — the pure rules for "I'm not training normally these days, and
here is what I'm actually doing instead".

The gap this closes: the morning report happened to know about a vacation (it reads
yesterday's report and the coach memory, so a mention leaked forward), while the Sunday
digest — the one surface whose whole job is judging the week — did not. It scored a
deliberately empty week as "compliance 0%, ні, відстаєш". A missed week and a planned week
off look identical in the data; only the athlete knows which one it was, so the athlete has
to be able to say it, once, in a place every surface reads.

The second half of the feature is the *context*: "відпустка" alone still leaves the coach
guessing. Lying on a beach, a week of trekking and a kite week are three completely
different loads — the first detrains, the second is quiet volume on tired legs, the third is
daily upper-body/core fatigue with zero running. ``kind`` + a free-text ``note`` is what
turns "he didn't run" into "of course he didn't run, he was kiting six hours a day".

Only rules live here (validation, parsing, overlap maths, the prompt block); storage is
``app.db.away`` and the prompt wording is ``app.analysis.prompts.AWAY_BLOCK``.
"""
import datetime as dt
import re
from typing import Iterable, List, Optional

# What KIND of away this is. Closed and small, for the same reason NF-28's tag vocabulary is:
# a slug is a stable DB value, and an open taxonomy stops being useful for prompt ordering.
# The specifics ("кайт в Дахабі", "Карпати, 15 км/день") belong in the free-text note.
#
# slug → (emoji, Ukrainian label, what the coach should expect of the body, /away keywords)
KINDS: dict = {
    "rest": ("🏖", "відпочинок", "майже без навантаження — можливе легке розтренування",
             ("відпочинок", "лежат", "пляж", "нічого", "rest", "chill")),
    "active": ("🥾", "активний відпочинок",
               "багато низькоінтенсивних годин на ногах (ходьба/трекінг) — втома є, "
               "бігової роботи немає",
               ("трекінг", "треккінг", "похід", "хайк", "гори", "прогулянк", "hike",
                "trek", "walking")),
    "sport": ("🪁", "інший спорт",
              "щоденне навантаження від іншого спорту — тіло НЕ відпочиває, просто "
              "навантажується інакше",
              ("кайт", "серф", "лижі", "сноуборд", "теніс", "вело", "дайв", "kite",
               "surf", "ski")),
    "work": ("💼", "робоча поїздка",
             "збитий режим і сон, тренування — за залишковим принципом",
             ("робоч", "відрядж", "конференц", "work", "business")),
}

DEFAULT_KIND = "rest"
KIND_ORDER: List[str] = list(KINDS)

MAX_NOTE_CHARS = 200
# A period longer than this is not "away", it's a life change — and an unbounded row would
# silence the coach's compliance judgement indefinitely, which is exactly the failure mode
# this feature must not create.
MAX_DAYS = 120
# How far ahead a period may be declared. Beyond this it's a plan, not a context.
MAX_FUTURE_DAYS = 365
# A period that ended within this many days is still worth telling the coach about: the
# Sunday digest runs after the week it is judging, and "he got back on Wednesday" explains
# the week's shape as much as being away does.
RECENT_DAYS = 14


def label(kind: str) -> str:
    """"🪁 інший спорт" — for buttons, the profile page and proposal text."""
    emoji, text, _expect, _kw = KINDS.get(kind, ("•", kind, "", ()))
    return f"{emoji} {text}"


def expectation(kind: str) -> str:
    """One line on what this kind of week does to a body — the thing the coach needs in
    order to read the numbers, kept in code so all four surfaces word it identically."""
    return KINDS.get(kind, KINDS[DEFAULT_KIND])[2]


def parse_kind(text: str) -> Optional[str]:
    """The kind mentioned in free text, or ``None``. Used by ``/away`` (so "кайт тиждень"
    lands on ``sport`` without the user learning a vocabulary) and as a fallback when the
    plan-edit model returns a note but no kind.

    Keywords match at a WORD START, not anywhere: a bare substring test reads "догори" as
    "гори" and files a beach week under trekking."""
    low = (text or "").lower()
    for slug in KIND_ORDER:
        if any(re.search(r"\b" + re.escape(kw), low) for kw in KINDS[slug][3]):
            return slug
    return None


_ISO_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_DM_RE = re.compile(r"\b(\d{1,2})[.\/](\d{1,2})(?:[.\/](\d{2,4}))?\b")
_DAYS_RE = re.compile(r"(?:на\s+)?(\d{1,3})\s*(?:дн[а-яіїєґ]*|days?)\b", re.IGNORECASE)


def _date_from_dm(day: int, month: int, year: Optional[int], today: dt.date
                  ) -> Optional[dt.date]:
    """A bare "24.08" is the NEXT occurrence of that day — a period typed on 16 August for
    "20.12-28.12" means this December, and one typed on 2 January for "28.12" means the
    December just gone only if that reading is closer. Rolling forward is the right default
    because an away period is overwhelmingly declared before it happens."""
    if year is not None:
        if year < 100:
            year += 2000
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    for y in (today.year, today.year + 1):
        try:
            cand = dt.date(y, month, day)
        except ValueError:
            return None
        # Allow a slightly-past start (declared on the third day of a trip) before rolling
        # the whole thing into next year.
        if (today - cand).days <= 14:
            return cand
    return None


def parse_dates(text: str, today: dt.date) -> tuple:
    """``(start, end)`` parsed out of a free-text ``/away`` argument, either ``None``.

    Understands ``2026-08-16``/``16.08``/``16.08.2026`` (one or two of them, in either
    ``-``/``—``/``до`` separated form) and a duration (``на 7 днів``) that fills in the
    missing end. A start with neither an end nor a duration is a one-day period, which is
    almost never what someone means — the caller says so rather than guessing a week."""
    found: List[dt.date] = []
    for m in _ISO_RE.finditer(text or ""):
        try:
            found.append(dt.date.fromisoformat(m.group(1)))
        except ValueError:
            continue
    if not found:
        for m in _DM_RE.finditer(text or ""):
            d = _date_from_dm(int(m.group(1)), int(m.group(2)),
                              int(m.group(3)) if m.group(3) else None, today)
            if d is not None:
                found.append(d)
    if not found:
        # "на 10 днів" with no dates at all — starts today.
        dm = _DAYS_RE.search(text or "")
        if dm:
            n = int(dm.group(1))
            return today, today + dt.timedelta(days=max(1, n) - 1)
        return None, None

    start = found[0]
    if len(found) > 1:
        end = found[1]
        # A second date that fell BEFORE the first (e.g. "28.12-04.01" read across the new
        # year) means the period spans a year boundary.
        if end < start:
            try:
                end = end.replace(year=end.year + 1)
            except ValueError:
                pass
        return start, end
    dm = _DAYS_RE.search(text or "")
    if dm:
        return start, start + dt.timedelta(days=max(1, int(dm.group(1))) - 1)
    return start, None


def strip_meta(text: str) -> str:
    """The free-text note left after the dates/duration are taken out — what the user
    actually said they'll be doing, which is the whole point of the feature."""
    s = _ISO_RE.sub(" ", text or "")
    s = _DM_RE.sub(" ", s)
    s = _DAYS_RE.sub(" ", s)
    # Whole words only — an unanchored "до" eats the middle of "догори".
    s = re.sub(r"\b(?:до|по|from|to)\b", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip(" ,;–—-")


class AwayError(ValueError):
    """A period the user asked for that we refuse to store, with a message to show them."""


def normalize(start, end, kind: Optional[str] = None, note: Optional[str] = None,
              *, today: Optional[dt.date] = None) -> dict:
    """Validate one period into storage shape (``{start_date, end_date, kind, note}``) or
    raise :class:`AwayError` with a user-facing reason. Every writer — the bot command, the
    web form and the plan-edit proposal — goes through this, so an LLM-proposed period is
    held to exactly the same bounds as a hand-typed one."""
    today = today or dt.date.today()
    s, e = _coerce_date(start), _coerce_date(end)
    if s is None:
        raise AwayError("Не зрозумів дати. Напр.: /away 16.08-24.08 кайт")
    if e is None:
        raise AwayError("Потрібна дата кінця: /away 16.08-24.08 кайт (або «на 7 днів»)")
    if e < s:
        s, e = e, s
    span = (e - s).days + 1
    if span > MAX_DAYS:
        raise AwayError(f"Задовгий період ({span} дн). Максимум {MAX_DAYS} днів.")
    if (s - today).days > MAX_FUTURE_DAYS:
        raise AwayError("Задалеко в майбутньому — заплануй ближче до дати.")
    note = (note or "").strip()[:MAX_NOTE_CHARS] or None
    kind = kind if kind in KINDS else (parse_kind(note or "") or DEFAULT_KIND)
    return {"start_date": s.isoformat(), "end_date": e.isoformat(),
            "kind": kind, "note": note}


def _coerce_date(v) -> Optional[dt.date]:
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v))
    except (TypeError, ValueError):
        return None


def from_op(op, *, today: Optional[dt.date] = None) -> Optional[dict]:
    """Storage shape from an LLM-proposed ``AwayOp`` (or an equivalent dict), or ``None``.

    Never raises: a model slip in a field nobody asked about must not take down the plan
    edit it rode along with. The bounds are the SAME :func:`normalize` a hand-typed period
    goes through — an away period proposed by Claude gets no extra trust."""
    if op is None:
        return None
    data = op if isinstance(op, dict) else getattr(op, "model_dump", lambda: {})()
    note = (data.get("note") or "").strip() or None
    try:
        return normalize(data.get("start"), data.get("end"),
                         data.get("kind") or parse_kind(note or ""), note, today=today)
    except AwayError:
        return None


def covers(period: dict, day) -> bool:
    """Is ``day`` inside this period (inclusive)?"""
    d = _coerce_date(day)
    s, e = _coerce_date(period.get("start_date")), _coerce_date(period.get("end_date"))
    return bool(d and s and e and s <= d <= e)


def days_in_range(period: dict, start, end) -> int:
    """How many days of ``period`` fall inside ``[start, end]`` — the digest's "was this
    week actually a training week?" number, computed in Python rather than left to the
    model's date arithmetic (the same rule the daily report's relative labels follow)."""
    s, e = _coerce_date(period.get("start_date")), _coerce_date(period.get("end_date"))
    a, b = _coerce_date(start), _coerce_date(end)
    if not (s and e and a and b):
        return 0
    lo, hi = max(s, a), min(e, b)
    return max(0, (hi - lo).days + 1)


def status(period: dict, today) -> str:
    """``active`` / ``upcoming`` / ``past`` for one period, from the user's own today."""
    d = _coerce_date(today) or dt.date.today()
    if covers(period, d):
        return "active"
    s = _coerce_date(period.get("start_date"))
    return "upcoming" if (s and s > d) else "past"


def current(periods: Iterable[dict], today) -> Optional[dict]:
    """The period covering ``today``, if any (earliest start wins on an overlap)."""
    d = _coerce_date(today) or dt.date.today()
    hits = [p for p in periods or [] if covers(p, d)]
    return min(hits, key=lambda p: p["start_date"]) if hits else None


def to_context(periods: Iterable[dict], today, *,
               week_start=None, week_end=None) -> Optional[dict]:
    """The prompt block, or ``None`` when there is nothing to say.

    ``None`` rather than an empty structure, exactly like ``app.profile.to_context``: a user
    who has never declared a period must get byte-for-byte the prompts they got before this
    feature existed, so the field is absent, not present-and-empty.

    Carries at most three periods — the one covering today, the next one coming up, and (when
    a week window is given) any period that overlaps that week. ``days_in_week`` is what
    stops the digest reading a deliberate zero as a failure."""
    d = _coerce_date(today) or dt.date.today()
    ws, we = _coerce_date(week_start), _coerce_date(week_end)
    rows = [p for p in periods or [] if p.get("start_date") and p.get("end_date")]

    chosen: List[dict] = []
    seen = set()

    def _take(p: Optional[dict]):
        if p is None:
            return
        key = (p["start_date"], p["end_date"])
        if key in seen:
            return
        seen.add(key)
        chosen.append(p)

    _take(current(rows, d))
    upcoming = sorted((p for p in rows if status(p, d) == "upcoming"),
                      key=lambda p: p["start_date"])
    _take(upcoming[0] if upcoming else None)
    if ws and we:
        for p in sorted(rows, key=lambda p: p["start_date"]):
            if days_in_range(p, ws, we) > 0:
                _take(p)
    else:
        recent = [p for p in rows if status(p, d) == "past"
                  and 0 <= (d - _coerce_date(p["end_date"])).days <= RECENT_DAYS]
        _take(max(recent, key=lambda p: p["end_date"]) if recent else None)

    if not chosen:
        return None

    out = []
    for p in chosen[:3]:
        st = status(p, d)
        end_d = _coerce_date(p["end_date"])
        start_d = _coerce_date(p["start_date"])
        item = {
            "start": p["start_date"],
            "end": p["end_date"],
            "kind": p.get("kind") or DEFAULT_KIND,
            "kind_label": label(p.get("kind") or DEFAULT_KIND),
            "expect": expectation(p.get("kind") or DEFAULT_KIND),
            "status": st,
            "days_total": (end_d - start_d).days + 1,
        }
        if p.get("note"):
            item["note"] = p["note"]
        if st == "active":
            item["days_left"] = (end_d - d).days
        elif st == "upcoming":
            item["days_until"] = (start_d - d).days
        else:
            item["days_since_end"] = (d - end_d).days
        if ws and we:
            item["days_in_week"] = days_in_range(p, ws, we)
        out.append(item)
    return {"periods": out}


def describe(period: dict) -> str:
    """One human line for a Telegram reply / the proposal card: "🪁 інший спорт · 16.08-24.08
    · кайт тиждень в Дахабі"."""
    s, e = _coerce_date(period.get("start_date")), _coerce_date(period.get("end_date"))
    span = (f"{s.strftime('%d.%m')}-{e.strftime('%d.%m')}" if s and e else "")
    parts = [label(period.get("kind") or DEFAULT_KIND), span]
    if period.get("note"):
        parts.append(str(period["note"]))
    return " · ".join(p for p in parts if p)
