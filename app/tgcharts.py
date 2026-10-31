"""NF-32 · Charts as a picture in Telegram — what to draw for each message, and sending it.

The geometry and the PNG conversion live in :mod:`app.charts`; this module only decides
which numbers each message's picture shows (all already stored — **0 LLM, 0 Garmin**) and
delivers it best-effort:

- the weekly digest → weekly run km (bars) + daily HRV and sleep;
- the auto-analysed run → the pace / HR / elevation curves of that run;
- ``/compare`` → weekly km now vs the same weeks a year ago, on the same axes;
- ``/insights`` → the strongest numeric finding as a scatter of the paired days.

The one rule everything here obeys: **the picture never costs the message.** Every builder
returns ``None`` instead of raising, :func:`send_chart` swallows a failed upload, and the
callers send their text first — so a missing ``cairosvg``, a render crash or a Telegram
hiccup leaves the delivery exactly as it was before this feature.
"""
import datetime as dt
import logging
from typing import Optional

from fastapi.concurrency import run_in_threadpool

from app import charts
from app.core.config import settings

logger = logging.getLogger("tgcharts")

DIGEST_VOLUME_WEEKS = 8     # bars: two months of weekly km is the progression context
DIGEST_RECOVERY_DAYS = 28   # lines: four weeks of HRV / sleep

_KM = "#6cb6ff"
_HRV = "#7aa2f7"
_SLEEP = "#9ece6a"
_PACE = "#6cb6ff"
_HR = "#ff7b72"
_ELEV = "#7ee787"
_SCATTER = "#d2a8ff"


async def _render(svg: Optional[str]) -> Optional[bytes]:
    """SVG → PNG off the event loop (cairosvg is CPU-bound, ~50–100 ms on a Pi)."""
    if not svg:
        return None
    return await run_in_threadpool(charts.to_png, svg)


def _short(date_s: Optional[str]) -> str:
    try:
        d = dt.date.fromisoformat(date_s)
    except (TypeError, ValueError):
        return ""
    return f"{d.day:02d}.{d.month:02d}"


def digest_svg(weekly_volume: list, recovery: list) -> Optional[str]:
    """The digest's picture from the two reads it already makes (pure — testable without a
    DB). Any panel with too little data simply drops out; none left → None."""
    panels = [
        charts.bar_panel("Біг, км/тиждень", [w.get("km") for w in weekly_volume or []],
                         color=_KM, fmt="f1"),
        charts.line_panel("HRV", [r.get("hrv_avg") for r in recovery or []],
                          color=_HRV, fmt="int"),
        charts.line_panel("Сон, год", [r.get("sleep_h") for r in recovery or []],
                          color=_SLEEP, fmt="f1"),
    ]
    first = _short(recovery[0].get("date")) if recovery else ""
    last = _short(recovery[-1].get("date")) if recovery else ""
    return charts.chart_svg("Тиждень: обсяг і відновлення", panels, first, last)


def activity_svg(series: Optional[list], title: str = "Пробіжка") -> Optional[str]:
    """One run's (or ride's) curves, the same channels the activity page draws."""
    if not series:
        return None
    is_ride = any(p.get("spd") is not None or p.get("pw") is not None for p in series)
    if is_ride:
        panels = [
            charts.line_panel("Швидкість, км/год", [p.get("spd") for p in series],
                              color=_PACE, fmt="f1"),
            charts.line_panel("Потужність, Вт", [p.get("pw") for p in series],
                              color="#f0b429", fmt="int"),
        ]
    else:
        panels = [charts.line_panel("Темп, хв/км", [p.get("p") for p in series],
                                    color=_PACE, fmt="pace")]
    panels += [
        charts.line_panel("Пульс", [p.get("hr") for p in series], color=_HR, fmt="int"),
        charts.line_panel("Висота, м", [p.get("e") for p in series], color=_ELEV, fmt="int"),
    ]
    dists = [p.get("d") for p in series if isinstance(p.get("d"), (int, float))]
    first = f"{dists[0]:.1f} км" if dists else ""
    last = f"{dists[-1]:.1f} км" if dists else ""
    return charts.chart_svg(title, panels, first, last)


def compare_svg(current_km: list, past_km: list, weeks: int) -> Optional[str]:
    """Weekly km now (solid) vs a year ago (dashed), bucket for bucket. A window with no runs
    at all on either side says nothing a picture should show → None."""
    if not any(current_km or []) or not any(past_km or []):
        return None
    panel = charts.line_panel("Біг, км/тиждень", current_km, color=_KM, fmt="f1",
                              overlay=past_km, names=("зараз", "рік тому"))
    return charts.chart_svg(f"Ти проти себе рік тому · {weeks} тиж", [panel],
                            "1-й тиждень", f"{weeks}-й тиждень")


def insights_svg(history: list, findings: list) -> Optional[str]:
    """The strongest *numeric* finding as a scatter of the days it was computed over.
    Lifestyle-tag findings (NF-28) are binary — a scatter of 0/1 says nothing — so they
    are skipped; nothing numeric → None."""
    from app import correlations

    for f in findings or []:
        if str(f.get("x", "")).startswith("tag:"):
            continue
        xs, ys = correlations.paired(history, f["x"], f["y"], f.get("lag", 0))
        name_x, name_y = correlations.name(f["x"]), correlations.name(f["y"])
        lag = f.get("lag", 0)
        label = f"{name_x} → {name_y}" + (f" (через {lag} дн)" if lag else "")
        panel = charts.scatter_panel(f"r = {f.get('r')}", xs, ys, color=_SCATTER,
                                     x_label=name_x, y_label=name_y)
        return charts.chart_svg(label, [panel])
    return None


async def digest_chart(session, user_id: int) -> Optional[bytes]:
    from app.garmin import repository

    weekly = await repository.weekly_run_volume(session, user_id, weeks=DIGEST_VOLUME_WEEKS)
    recovery = await repository.read_history(session, user_id, days=DIGEST_RECOVERY_DAYS)
    return await _render(digest_svg(weekly, recovery))


async def activity_chart(act) -> Optional[bytes]:
    kind = "Заїзд" if "cycl" in (getattr(act, "type", None) or "") else "Пробіжка"
    date = _short(getattr(act, "date", None))
    return await _render(activity_svg(getattr(act, "series", None),
                                      f"{kind} · {date}" if date else kind))


async def compare_chart(session, user_id: int, weeks: int,
                        today: Optional[dt.date] = None) -> Optional[bytes]:
    from app import compare as compare_mod
    from app.garmin import repository

    cur_s, cur_e, past_s, past_e = compare_mod.window_pair(today or dt.date.today(), weeks)
    current = await repository.weekly_km_between(session, user_id, cur_s, cur_e)
    past = await repository.weekly_km_between(session, user_id, past_s, past_e)
    return await _render(compare_svg(current, past, weeks))


async def insights_chart(session, user_id: int) -> Optional[bytes]:
    from app import correlations
    from app.analysis.reports import INSIGHTS_WINDOW_DAYS
    from app.garmin import repository

    history = await repository.read_history(session, user_id, days=INSIGHTS_WINDOW_DAYS)
    return await _render(insights_svg(history, correlations.find_correlations(history)))


async def send_chart(bot, chat_id, build, *, caption: Optional[str] = None) -> bool:
    """Build (an awaitable from the ``*_chart`` builders above) and send one picture.
    Best-effort end to end: switched off, no picture, a builder crash or a failed upload all
    return False and send nothing — the caller's text has already gone out."""
    if not settings.CHARTS_IN_TELEGRAM or not chat_id:
        if hasattr(build, "close"):
            build.close()   # don't leave an un-awaited coroutine behind
        return False
    try:
        png = await build
        if not png:
            return False
        await bot.send_photo(chat_id, photo=png, caption=caption)
        return True
    except Exception:  # noqa: BLE001 — see the module docstring: never cost the message
        logger.exception(f"CHARTS send failed chat={chat_id}")
        return False
