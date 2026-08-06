"""Inline-SVG chart helpers — no JS bundling, no CDN, renders server-side.

Shared by the admin DB browser (``app/routers/admin.py``), the per-user data browser
(``app/routers/me.py``) and the dashboard (``app/routers/dashboard.py``, EP-04).
Extracted from ``admin.py`` (EP-04) so a new page reuses the same geometry instead of
growing its own chart stack.
"""

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
