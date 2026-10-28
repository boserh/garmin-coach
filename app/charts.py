"""Inline-SVG chart helpers — no JS bundling, no CDN, renders server-side.

Shared by the admin DB browser (``app/routers/admin.py``), the per-user data browser
(``app/routers/me.py``) and the dashboard (``app/routers/dashboard.py``, EP-04).
Extracted from ``admin.py`` (EP-04) so a new page reuses the same geometry instead of
growing its own chart stack.

NF-32 adds the second output: :func:`chart_svg` + :func:`to_png` turn the same kind of
series into a PNG for Telegram (:mod:`app.tgcharts` decides what to draw).
"""
import logging
from typing import List, Optional

logger = logging.getLogger("charts")

# geometry shared by every sparkline in the app
SVG_W, SVG_H, SVG_PAD = 720, 120, 22


def series(values):
    """Scale a chronological value list to SVG coords for a trend sparkline.
    Returns None when there are fewer than 2 data points to draw."""
    pairs = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return None
    n = len(values)
    ys = [v for _, v in pairs]
    ymin, ymax = min(ys), max(ys)
    span = (ymax - ymin) or 1.0

    def px(i):
        return SVG_PAD + (i / (n - 1)) * (SVG_W - 2 * SVG_PAD)

    def py(v):
        return SVG_H - SVG_PAD - ((v - ymin) / span) * (SVG_H - 2 * SVG_PAD)

    dots = [(round(px(i), 1), round(py(v), 1)) for i, v in pairs]
    points = " ".join(f"{x},{y}" for x, y in dots)
    return {"points": points, "dots": dots, "ymin": ymin, "ymax": ymax,
            "last": ys[-1], "W": SVG_W, "H": SVG_H}


def trend_series(values, labels):
    """Like :func:`series` but also carries each point's raw value + a label (``pts``:
    x as a 0..1 fraction, ``v``, ``lbl``) so a chart can show a value on hover — used
    for date-labelled trends (HRV/sleep/RHR/stress over N days). None if < 2 points."""
    pairs = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return None
    n = len(values)
    ys = [v for _, v in pairs]
    ymin, ymax = min(ys), max(ys)
    span = (ymax - ymin) or 1.0

    def px(i):
        return SVG_PAD + (i / (n - 1)) * (SVG_W - 2 * SVG_PAD)

    def py(v):
        return SVG_H - SVG_PAD - ((v - ymin) / span) * (SVG_H - 2 * SVG_PAD)

    dots = [(round(px(i), 1), round(py(v), 1)) for i, v in pairs]
    points = " ".join(f"{x},{y}" for x, y in dots)
    pts = [{"x": round(i / (n - 1), 4), "v": v, "lbl": labels[i] if i < len(labels) else ""}
           for i, v in pairs]
    return {"points": points, "dots": dots, "pts": pts, "ymin": ymin, "ymax": ymax,
            "last": ys[-1], "W": SVG_W, "H": SVG_H}


def run_series(values, dists):
    """Like :func:`series` but also carries each point's raw value + distance
    (``pts``, x as a 0..1 fraction) so the activity detail page can show them on
    hover."""
    pairs = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return None
    n = len(values)
    ys = [v for _, v in pairs]
    ymin, ymax = min(ys), max(ys)
    span = (ymax - ymin) or 1.0

    def px(i):
        return SVG_PAD + (i / (n - 1)) * (SVG_W - 2 * SVG_PAD)

    def py(v):
        return SVG_H - SVG_PAD - ((v - ymin) / span) * (SVG_H - 2 * SVG_PAD)

    points = " ".join(f"{round(px(i), 1)},{round(py(v), 1)}" for i, v in pairs)
    pts = [{"x": round(i / (n - 1), 4), "v": v,
            "d": dists[i] if i < len(dists) else None} for i, v in pairs]
    return {"points": points, "pts": pts, "ymin": ymin, "ymax": ymax,
            "last": ys[-1], "W": SVG_W, "H": SVG_H}


def run_charts(activity_series):
    """Sparklines for an activity's per-point series ([{d, p, hr, e?}, ...] for a run, or
    EP-10's ``[{d, spd, pw, hr, e?}, ...]`` for a ride — picked by which keys are present).
    Returns (charts, first_km, last_km) for the activity detail page. Each chart
    carries a ``fmt`` hint (pace/speed/power/hr/elev) so the hover tooltip formats the
    value right. EP-15: a third ``elev`` sparkline (altitude profile) appears whenever the
    series carries elevation — absent entirely on old, pre-backfill series (no ``e`` key)."""
    if not activity_series:
        return [], "", ""
    dists = [p.get("d") for p in activity_series]
    is_ride = any(p.get("spd") is not None or p.get("pw") is not None
                  for p in activity_series)
    if is_ride:
        defs = [
            ("Швидкість, км/год", "#6cb6ff", "speed", [p.get("spd") for p in activity_series]),
            ("Потужність, Вт", "#f0b429", "power", [p.get("pw") for p in activity_series]),
            ("Пульс", "#ff7b72", "hr", [p.get("hr") for p in activity_series]),
        ]
    else:
        defs = [
            ("Темп, хв/км", "#6cb6ff", "pace", [p.get("p") for p in activity_series]),
            ("Пульс", "#ff7b72", "hr", [p.get("hr") for p in activity_series]),
            # NF-25: cadence, when the watch reported it — the channel that shows form
            # falling away late in a run. Absent (like elevation) on series without it.
            ("Каденс, кр/хв", "#d2a8ff", "cadence",
             [p.get("cad") for p in activity_series]),
        ]
    defs.append(("Висота, м", "#7ee787", "elev", [p.get("e") for p in activity_series]))
    charts = [{"label": lbl, "color": c, "fmt": fmt, "s": s}
              for lbl, c, fmt, vals in defs if (s := run_series(vals, dists))]
    valid = [d for d in dists if d is not None]
    first = f"{valid[0]:.1f} км" if valid else ""
    last = f"{valid[-1]:.1f} км" if valid else ""
    return charts, first, last


def bar_series(values, labels):
    """Scale a chronological value list to SVG coords for a **bar** chart (UI-06).

    Weekly tonnage is a per-week total, not a continuous signal — drawing it as a line
    implies values in between that don't exist. Bars start at zero (a truncated axis
    would make a 5% week-over-week change look like a doubling), so ``ymin`` is always
    0 and only ``ymax`` scales. Returns ``None`` with nothing to draw.

    ``bars`` carries each rect's ``x/y/w/h`` plus the raw ``v``/``lbl``, and ``pts`` uses
    the same 0..1-fraction shape as :func:`trend_series` so the shared tooltip component
    in ``app.js`` works on a bar chart with no extra code.
    """
    vals = [(i, float(v)) for i, v in enumerate(values) if v is not None]
    if not vals:
        return None
    n = len(values)
    ymax = max(v for _i, v in vals) or 1.0
    inner_w = SVG_W - 2 * SVG_PAD
    inner_h = SVG_H - 2 * SVG_PAD
    # A little air between bars, and never wider than the slot itself.
    slot = inner_w / n
    width = max(2.0, slot * 0.68)

    bars = []
    pts = []
    for i, v in vals:
        cx = SVG_PAD + slot * (i + 0.5)
        h = (v / ymax) * inner_h
        bars.append({
            "x": round(cx - width / 2, 1), "y": round(SVG_H - SVG_PAD - h, 1),
            "w": round(width, 1), "h": round(max(h, 0.5), 1),
            "v": v, "lbl": labels[i] if i < len(labels) else "",
        })
        pts.append({"x": round((cx - SVG_PAD) / inner_w if inner_w else 0, 4), "v": v,
                    "lbl": labels[i] if i < len(labels) else ""})
    return {"bars": bars, "pts": pts, "ymin": 0.0, "ymax": ymax,
            "last": vals[-1][1], "W": SVG_W, "H": SVG_H}


def shade_zones(activity_series, ranges):
    """SVG bands marking distance windows on an activity chart (UI-08).

    ``activity_series`` is the run's per-point series (each point's ``d`` is cumulative
    metres); ``ranges`` is ``[{from_m, to_m, hit}]`` — the actual distance windows of the
    scored intervals. Returns ``[{x, w, hit}]`` in the same SVG coordinate space
    :func:`run_series` draws in, so the bands line up with the curve exactly.

    Why distance and not lap index: the curve's x axis is the series' own point index,
    which is sampled by distance, so a band placed by lap number would drift from the
    line it is supposed to mark. Ranges the series doesn't reach are dropped rather than
    clamped — a band that isn't real shouldn't be drawn.
    """
    dists = [p.get("d") for p in (activity_series or [])]
    usable = [d for d in dists if isinstance(d, (int, float))]
    if len(usable) < 2 or not ranges:
        return []
    total = max(usable)
    if total <= 0:
        return []

    def x_of(metres):
        return SVG_PAD + min(1.0, max(0.0, metres / total)) * (SVG_W - 2 * SVG_PAD)

    out = []
    for r in ranges:
        a, b = r.get("from_m"), r.get("to_m")
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)) or b <= a:
            continue
        if a >= total:
            continue
        x0, x1 = x_of(a), x_of(b)
        if x1 - x0 < 0.5:
            continue
        out.append({"x": round(x0, 1), "w": round(x1 - x0, 1), "hit": bool(r.get("hit"))})
    return out


# ---------- NF-32: the same charts as a PNG for Telegram ----------
#
# The web draws its charts in Jinja templates around the coordinate dicts above; Telegram
# needs a picture. So this half builds one standalone SVG document — a title plus a stack of
# panels — and hands it to cairosvg. Pure, offline conversion: no headless browser in the
# production process (Playwright is a dev-only dependency for the layout guards).
#
# Telegram re-compresses every photo and shows it in a dark chat bubble, which decides the
# style: a dark background, thick lines, big labels, no hairlines.

PNG_W = 800
PNG_TITLE_H = 64
PNG_PANEL_H = 200
PNG_PAD_X = 28
PNG_SCALE = 1.5          # crisper text after Telegram's recompression…
PNG_MAX_BYTES = 200_000  # …but the picture stays small enough for a Pi and a phone plan

_BG = "#0d1117"
_GRID = "#30363d"
_TEXT = "#e6edf3"
_MUTED = "#8b949e"
_FONT = "DejaVu Sans, Inter, Arial, sans-serif"


def _fmt_value(v: float, fmt: str) -> str:
    """Axis/end label for one value. ``pace`` is min/km as a float → "5:07"."""
    if fmt == "pace":
        total = int(round(v * 60))
        return f"{total // 60}:{total % 60:02d}"
    if fmt == "int":
        return str(int(round(v)))
    return f"{v:.1f}"


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def line_panel(label: str, values, *, color: str, fmt: str = "f1", overlay=None,
               names=None) -> Optional[dict]:
    """A line panel for :func:`chart_svg`. ``overlay`` is an optional second series drawn on
    the same axes (``/compare``'s "a year ago"); ``names`` labels the two for the legend.
    None when the main series has fewer than 2 points — a one-point "trend" is worse than no
    picture at all."""
    if sum(1 for v in (values or []) if v is not None) < 2:
        return None
    return {"kind": "line", "label": label, "color": color, "fmt": fmt,
            "values": list(values), "overlay": list(overlay) if overlay else None,
            "names": names}


def bar_panel(label: str, values, *, color: str, fmt: str = "f1") -> Optional[dict]:
    """A bar panel (per-week totals — bars from zero, see :func:`bar_series`). None below 2
    non-empty bars."""
    if sum(1 for v in (values or []) if v is not None) < 2:
        return None
    return {"kind": "bar", "label": label, "color": color, "fmt": fmt, "values": list(values)}


def scatter_panel(label: str, xs, ys, *, color: str, x_label: str = "",
                  y_label: str = "") -> Optional[dict]:
    """A scatter panel — the honest picture of a correlation (NF-02): every paired day as a
    dot, no fitted line implying more than the statistics say. None below 3 pairs."""
    pairs = [(float(x), float(y)) for x, y in zip(xs or [], ys or [])
             if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    return {"kind": "scatter", "label": label, "color": color, "pairs": pairs,
            "x_label": x_label, "y_label": y_label}


def _panel_svg(p: dict, top: float) -> List[str]:
    """SVG fragments for one panel whose box starts at ``top``."""
    out = []
    label_y = top + 30
    x0, x1 = PNG_PAD_X + 64, PNG_W - PNG_PAD_X
    y0, y1 = top + 48, top + PNG_PANEL_H - 22   # plot area (y grows downward)
    out.append(f'<text x="{PNG_PAD_X}" y="{label_y}" font-size="22" font-weight="bold" '
               f'fill="{p["color"]}">{_esc(p["label"])}</text>')
    out.append(f'<line x1="{x0}" y1="{y1}" x2="{x1}" y2="{y1}" stroke="{_GRID}" '
               f'stroke-width="2"/>')

    if p["kind"] == "scatter":
        xs = [a for a, _b in p["pairs"]]
        ys = [b for _a, b in p["pairs"]]
        xmin, xmax = min(xs), max(xs)
        ymin, ymax = min(ys), max(ys)
        xspan, yspan = (xmax - xmin) or 1.0, (ymax - ymin) or 1.0
        for a, b in p["pairs"]:
            cx = x0 + (a - xmin) / xspan * (x1 - x0)
            cy = y1 - (b - ymin) / yspan * (y1 - y0)
            out.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="6" fill="{p["color"]}" '
                       f'fill-opacity="0.75"/>')
        out.append(f'<text x="{PNG_PAD_X}" y="{y0 + 8}" font-size="18" fill="{_MUTED}">'
                   f'{_fmt_value(ymax, "f1")}</text>')
        out.append(f'<text x="{PNG_PAD_X}" y="{y1}" font-size="18" fill="{_MUTED}">'
                   f'{_fmt_value(ymin, "f1")}</text>')
        out.append(f'<text x="{x1}" y="{label_y}" font-size="18" fill="{_MUTED}" '
                   f'text-anchor="end">↑ {_esc(p["y_label"])} · → {_esc(p["x_label"])}</text>')
        return out

    values = p["values"]
    series_all = [v for v in values if v is not None]
    if p.get("overlay"):
        series_all += [v for v in p["overlay"] if v is not None]
    if p["kind"] == "bar":
        ymin, ymax = 0.0, (max(series_all) or 1.0)
    else:
        ymin, ymax = min(series_all), max(series_all)
    span = (ymax - ymin) or 1.0
    n = max(len(values), len(p.get("overlay") or []))

    def px(i):
        return x0 + (i / (n - 1 if n > 1 else 1)) * (x1 - x0)

    def py(v):
        return y1 - (v - ymin) / span * (y1 - y0)

    out.append(f'<text x="{PNG_PAD_X}" y="{y0 + 8}" font-size="18" fill="{_MUTED}">'
               f'{_fmt_value(ymax, p["fmt"])}</text>')
    out.append(f'<text x="{PNG_PAD_X}" y="{y1}" font-size="18" fill="{_MUTED}">'
               f'{_fmt_value(ymin, p["fmt"])}</text>')

    if p["kind"] == "bar":
        slot = (x1 - x0) / len(values)
        width = max(4.0, slot * 0.68)
        for i, v in enumerate(values):
            if v is None:
                continue
            h = max((v - ymin) / span * (y1 - y0), 1.0)
            cx = x0 + slot * (i + 0.5)
            out.append(f'<rect x="{cx - width / 2:.1f}" y="{y1 - h:.1f}" width="{width:.1f}" '
                       f'height="{h:.1f}" rx="3" fill="{p["color"]}"/>')
        last = series_all[-1] if series_all else None
    else:
        def polyline(vals, color, dash=""):
            pts = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(vals)
                           if v is not None)
            return (f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="4" '
                    f'stroke-linejoin="round" stroke-linecap="round"{dash}/>')

        if p.get("overlay"):
            out.append(polyline(p["overlay"], _MUTED, ' stroke-dasharray="10 8"'))
        out.append(polyline(values, p["color"]))
        idx = max(i for i, v in enumerate(values) if v is not None)
        out.append(f'<circle cx="{px(idx):.1f}" cy="{py(values[idx]):.1f}" r="7" '
                   f'fill="{p["color"]}"/>')
        last = values[idx]
        if p.get("names"):
            # Two plain swatch+label pairs, right-aligned. Not one <text> with <tspan>s:
            # cairosvg lays an end-anchored tspan run over itself.
            cur, past = p["names"]
            ly = label_y - 7
            past_x = x1 - 150
            out.append(f'<line x1="{past_x}" y1="{ly}" x2="{past_x + 28}" y2="{ly}" '
                       f'stroke="{_MUTED}" stroke-width="4" stroke-dasharray="8 5"/>')
            out.append(f'<text x="{past_x + 36}" y="{label_y}" font-size="18" '
                       f'fill="{_MUTED}">{_esc(past)}</text>')
            cur_x = past_x - 130
            out.append(f'<line x1="{cur_x}" y1="{ly}" x2="{cur_x + 28}" y2="{ly}" '
                       f'stroke="{p["color"]}" stroke-width="4"/>')
            out.append(f'<text x="{cur_x + 36}" y="{label_y}" font-size="18" '
                       f'fill="{p["color"]}">{_esc(cur)}</text>')
    if last is not None and not p.get("names"):
        out.append(f'<text x="{x1}" y="{label_y}" font-size="22" font-weight="bold" '
                   f'text-anchor="end" fill="{_TEXT}">{_fmt_value(last, p["fmt"])}</text>')
    return out


def chart_svg(title: str, panels, x_first: str = "", x_last: str = "") -> Optional[str]:
    """One standalone SVG document: ``title`` over a vertical stack of panels built by
    :func:`line_panel`/:func:`bar_panel`/:func:`scatter_panel` (``None`` entries are dropped,
    so callers can pass them straight through). ``x_first``/``x_last`` label the shared time
    axis under the last panel. Returns None when no panel survived — the caller then sends
    no picture rather than an empty frame."""
    panels = [p for p in (panels or []) if p]
    if not panels:
        return None
    axis_h = 34 if (x_first or x_last) else 8
    height = PNG_TITLE_H + PNG_PANEL_H * len(panels) + axis_h
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{PNG_W}" height="{height}" '
        f'viewBox="0 0 {PNG_W} {height}" font-family="{_FONT}">',
        f'<rect width="{PNG_W}" height="{height}" fill="{_BG}"/>',
        f'<text x="{PNG_PAD_X}" y="42" font-size="28" font-weight="bold" fill="{_TEXT}">'
        f'{_esc(title)}</text>',
    ]
    for i, p in enumerate(panels):
        parts += _panel_svg(p, PNG_TITLE_H + i * PNG_PANEL_H)
    if x_first or x_last:
        y = height - 10
        parts.append(f'<text x="{PNG_PAD_X + 64}" y="{y}" font-size="18" fill="{_MUTED}">'
                     f'{_esc(x_first)}</text>')
        parts.append(f'<text x="{PNG_W - PNG_PAD_X}" y="{y}" font-size="18" fill="{_MUTED}" '
                     f'text-anchor="end">{_esc(x_last)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def to_png(svg: Optional[str]) -> Optional[bytes]:
    """Render an SVG document to PNG bytes, or None — never raises.

    ``cairosvg`` is the optional ``.[charts]`` extra (it needs the system ``libcairo``, which
    a Pi has to have installed), so its absence is a normal state, not an error: every caller
    treats None as "send the text alone". A picture over :data:`PNG_MAX_BYTES` is re-rendered
    at 1× and dropped if it still doesn't fit."""
    if not svg:
        return None
    try:
        import cairosvg
    except Exception:  # noqa: BLE001 — ImportError, or OSError when libcairo is missing
        logger.debug("CHARTS cairosvg unavailable — sending text only")
        return None
    try:
        for scale in (PNG_SCALE, 1.0):
            png = cairosvg.svg2png(bytestring=svg.encode("utf-8"), scale=scale)
            if png and len(png) <= PNG_MAX_BYTES:
                return png
        logger.warning("CHARTS png over size cap — dropped")
        return None
    except Exception:  # noqa: BLE001 — a picture must never cost the message it decorates
        logger.exception("CHARTS render failed")
        return None
