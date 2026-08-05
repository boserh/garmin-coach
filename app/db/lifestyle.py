"""NF-28 · user-scoped CRUD over ``lifestyle_logs`` + the tag vocabulary.

The vocabulary lives here (not in the bot) because three surfaces need the same list: the
evening keyboard, the ``/log`` text parser, and the correlation engine's variable names. A
tag slug is a stable DB value — renaming one silently orphans a year of history, so the
display label is the only thing that may change.

Deliberately six tags and no more. Every extra button is a tax on the one interaction the
whole feature depends on, and a diary nobody fills in correlates nothing.
"""
import datetime as dt
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import LifestyleLog

# slug → (emoji, short label, /log keywords). Keywords are lower-case substrings matched
# against a free-text /log argument, so "вчора пиво" and "алкоголь" both land on `alcohol`.
TAGS: dict = {
    "alcohol":  ("🍺", "алкоголь",     ("пиво", "алкоголь", "вино", "бухло", "beer")),
    "caffeine": ("☕", "кава пізно",   ("кава", "кофе", "caffeine", "кофеїн")),
    "late_meal": ("🍽", "пізня їжа",   ("їжа", "вечеря", "жер", "meal")),
    "stress":   ("😖", "стрес",        ("стрес", "нерви", "важкий день", "stress")),
    "travel":   ("✈️", "подорож",      ("подорож", "переліт", "дорога", "travel")),
    "sick":     ("🤒", "нездужання",   ("хворі", "застуда", "нездуж", "sick")),
}

TAG_ORDER: List[str] = list(TAGS)


def label(slug: str) -> str:
    """"🍺 алкоголь" — for buttons and finding text."""
    emoji, text, _ = TAGS.get(slug, ("•", slug, ()))
    return f"{emoji} {text}"


def parse_tags(text: str) -> List[str]:
    """Tag slugs mentioned in a free-text ``/log`` argument, in vocabulary order (so the
    stored list is canonical regardless of how the user typed it). Unknown words are simply
    ignored — this is a one-tap feature with a text fallback, not a parser to fight."""
    low = (text or "").lower()
    return [slug for slug in TAG_ORDER
            if any(kw in low for kw in TAGS[slug][2])]


BACKDATE_MAX_DAYS = 30


def parse_date(text: str, today: dt.date) -> Optional[dt.date]:
    """The date a ``/log`` argument refers to: "вчора"/"позавчора", an explicit
    ``YYYY-MM-DD``, or today by default. Returns ``None`` when an explicit date is outside
    the last :data:`BACKDATE_MAX_DAYS` days or in the future — backfilling a random month
    from memory is not data, and the caller says so instead of silently storing it."""
    low = (text or "").lower()
    if "позавчора" in low:
        return today - dt.timedelta(days=2)
    if "вчора" in low:
        return today - dt.timedelta(days=1)
    for word in low.split():
        try:
            d = dt.date.fromisoformat(word.strip(",.;"))
        except ValueError:
            continue
        if d > today or (today - d).days > BACKDATE_MAX_DAYS:
            return None
        return d
    return today


async def get_day(session: AsyncSession, user_id: int, date: str) -> Optional[LifestyleLog]:
    return (await session.execute(
        select(LifestyleLog).where(
            LifestyleLog.user_id == user_id, LifestyleLog.date == date)
    )).scalar_one_or_none()


async def upsert(session: AsyncSession, user_id: int, date: str, tags: List[str],
                 note: Optional[str] = None) -> LifestyleLog:
    """Write (or overwrite) one day's tags. Re-tapping a button replaces the day rather
    than appending, so a mistap is corrected by tapping again — the only correction UI a
    one-tap feature can afford. ``tags=[]`` is stored as an empty list, NOT as "no row":
    that's the control group the whole analysis rests on."""
    tags = [t for t in TAG_ORDER if t in set(tags or [])]   # canonical order, deduped
    row = await get_day(session, user_id, date)
    if row is None:
        row = LifestyleLog(user_id=user_id, date=date, tags=tags, note=note)
        session.add(row)
    else:
        row.tags = tags
        if note is not None:
            row.note = note
    await session.commit()
    return row


async def toggle_tag(session: AsyncSession, user_id: int, date: str, slug: str) -> List[str]:
    """Flip one tag on this day and return the resulting tag list. The evening keyboard is
    multi-select by nature (beer AND a late meal is one evening, not two), so a tap toggles
    instead of replacing."""
    row = await get_day(session, user_id, date)
    current = set((row.tags if row else None) or [])
    current.symmetric_difference_update({slug})
    return (await upsert(session, user_id, date, sorted(current))).tags


async def read_range(session: AsyncSession, user_id: int, days: int = 90) -> List[dict]:
    """``[{date, tags, note}, ...]`` oldest first over the last ``days`` days — the shape
    ``app.correlations`` merges into the daily-metric history."""
    cutoff = (dt.date.today() - dt.timedelta(days=days - 1)).isoformat()
    rows = (await session.execute(
        select(LifestyleLog).where(
            LifestyleLog.user_id == user_id, LifestyleLog.date >= cutoff
        ).order_by(LifestyleLog.date)
    )).scalars().all()
    return [{"date": r.date, "tags": list(r.tags or []), "note": r.note} for r in rows]


async def read_all(session: AsyncSession, user_id: int) -> List[dict]:
    """Every row this user owns — NF-13's data export (these are user-authored data, not
    derived cache)."""
    rows = (await session.execute(
        select(LifestyleLog).where(LifestyleLog.user_id == user_id)
        .order_by(LifestyleLog.date)
    )).scalars().all()
    return [
        {"date": r.date, "tags": list(r.tags or []), "note": r.note,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in rows
    ]
