"""NF-32 — charts as a PNG in Telegram.

What matters most is the negative space: a missing ``cairosvg``, a crashing renderer, too
little data or a failed upload must each leave the message exactly as it was — text, no
picture, no exception. The happy path (a real PNG under the size cap) only runs where the
optional ``.[charts]`` extra is installed."""
import datetime as dt
import math
import sys

import pytest

from app import charts, tgcharts
from app.core.config import settings

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _weekly(n=8):
    return [{"week": f"2026-W{30 + i}", "km": 20.0 + i} for i in range(n)]


def _recovery(n=28):
    start = dt.date(2026, 10, 1)
    return [{"date": (start + dt.timedelta(days=i)).isoformat(),
             "hrv_avg": 45 + 4 * math.sin(i / 3), "sleep_h": 7.0 + (i % 3) * 0.3}
            for i in range(n)]


def _run(n=60):
    return [{"d": i * 0.1, "p": 5.2 + 0.2 * math.sin(i / 4), "hr": 140 + i * 0.2,
             "e": 100 + 5 * math.sin(i / 8)} for i in range(n)]


def _cairosvg_works() -> bool:
    try:
        import cairosvg  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


needs_cairo = pytest.mark.skipif(not _cairosvg_works(), reason="optional .[charts] extra")


# ---------- the SVG side: pure, always runs ----------

def test_digest_svg_has_every_panel():
    svg = tgcharts.digest_svg(_weekly(), _recovery())
    assert svg.startswith("<svg") and svg.endswith("</svg>")
    for label in ("Біг, км/тиждень", "HRV", "Сон, год"):
        assert label in svg


def test_sparse_data_sends_no_picture():
    """One point is not a trend: no axis with a lonely dot, no picture at all."""
    assert tgcharts.digest_svg(_weekly(1), _recovery(1)) is None
    assert tgcharts.digest_svg([], []) is None
    assert tgcharts.activity_svg([]) is None
    assert tgcharts.activity_svg([{"d": 0.1, "p": 5.0, "hr": 150}]) is None
    assert charts.chart_svg("x", [None, None]) is None


def test_panel_without_data_drops_out_rest_survives():
    rec = [{"date": r["date"], "hrv_avg": r["hrv_avg"], "sleep_h": None} for r in _recovery()]
    svg = tgcharts.digest_svg([], rec)
    assert "HRV" in svg and "Сон, год" not in svg and "км/тиждень" not in svg


def test_activity_svg_formats_pace_as_minutes():
    svg = tgcharts.activity_svg(_run(), "Пробіжка")
    assert "Темп, хв/км" in svg and "Пульс" in svg and "Висота, м" in svg
    assert "5:" in svg            # 5.2 min/km → "5:12", not "5.2"
    assert "6.0 км" in svg or "5.9 км" in svg


def test_activity_svg_ride_uses_speed_and_power():
    ride = [{"d": i * 0.5, "spd": 28 + i % 4, "pw": 180 + i, "hr": 130} for i in range(20)]
    svg = tgcharts.activity_svg(ride)
    assert "Швидкість" in svg and "Потужність" in svg and "Темп" not in svg


def test_compare_svg_needs_runs_on_both_sides():
    assert tgcharts.compare_svg([10, 12, 0, 15], [0, 0, 0, 0], 4) is None
    svg = tgcharts.compare_svg([10, 12, 14, 15], [8, 9, 11, 10], 4)
    assert "рік тому" in svg and "stroke-dasharray" in svg


def test_insights_svg_skips_tag_findings_and_uses_the_same_pairs():
    history = [{"date": f"d{i}", "sleep_score": 60 + i % 20, "hrv_avg": 40 + i % 20}
               for i in range(40)]
    assert tgcharts.insights_svg(history, [{"x": "tag:alcohol", "y": "hrv_avg", "lag": 1,
                                            "r": -0.5}]) is None
    svg = tgcharts.insights_svg(history, [
        {"x": "tag:alcohol", "y": "hrv_avg", "lag": 1, "r": -0.5},
        {"x": "sleep_score", "y": "hrv_avg", "lag": 0, "r": 0.9},
    ])
    assert "r = 0.9" in svg and "оцінка сну" in svg
    assert svg.count("<circle") == 40


def test_labels_are_escaped():
    p = charts.line_panel("a<b & c", [1, 2], color="#fff")
    assert "a&lt;b &amp; c" in charts.chart_svg("t", [p])


# ---------- the PNG side ----------

@needs_cairo
def test_to_png_is_a_valid_small_png():
    for svg in (tgcharts.digest_svg(_weekly(), _recovery()),
                tgcharts.activity_svg(_run(400))):
        png = charts.to_png(svg)
        assert png[:8] == PNG_MAGIC
        assert len(png) < 200_000


def test_to_png_without_cairosvg_returns_none(monkeypatch):
    """The extra isn't installed (CI, a Pi without libcairo) → text only, no ImportError."""
    monkeypatch.setitem(sys.modules, "cairosvg", None)   # makes `import cairosvg` fail
    assert charts.to_png(tgcharts.digest_svg(_weekly(), _recovery())) is None


def test_to_png_render_failure_returns_none(monkeypatch):
    class Boom:
        @staticmethod
        def svg2png(**kw):
            raise RuntimeError("cairo exploded")

    monkeypatch.setitem(sys.modules, "cairosvg", Boom)
    assert charts.to_png("<svg/>") is None


def test_to_png_over_size_cap_is_dropped(monkeypatch):
    class Huge:
        @staticmethod
        def svg2png(**kw):
            return PNG_MAGIC + b"\0" * (charts.PNG_MAX_BYTES + 1)

    monkeypatch.setitem(sys.modules, "cairosvg", Huge)
    assert charts.to_png("<svg/>") is None


def test_to_png_none_svg():
    assert charts.to_png(None) is None


# ---------- delivery: the picture never costs the message ----------

class _Bot:
    def __init__(self, photo_error=None):
        self.texts, self.photos = [], []
        self.photo_error = photo_error

    async def send_message(self, chat_id, text, **kw):
        self.texts.append(text)

    async def send_photo(self, chat_id, photo=None, caption=None, **kw):
        if self.photo_error:
            raise self.photo_error
        self.photos.append(photo)


async def _png():
    return PNG_MAGIC + b"data"


async def test_send_chart_sends_photo():
    bot = _Bot()
    assert await tgcharts.send_chart(bot, 5, _png()) is True
    assert bot.photos == [PNG_MAGIC + b"data"]


async def test_send_chart_switched_off(monkeypatch):
    monkeypatch.setattr(settings, "CHARTS_IN_TELEGRAM", False)
    bot = _Bot()
    assert await tgcharts.send_chart(bot, 5, _png()) is False
    assert bot.photos == []


async def test_send_chart_swallows_builder_and_upload_failures():
    async def boom():
        raise RuntimeError("render died")

    bot = _Bot()
    assert await tgcharts.send_chart(bot, 5, boom()) is False
    bot = _Bot(photo_error=RuntimeError("telegram 400"))
    assert await tgcharts.send_chart(bot, 5, _png()) is False


async def test_render_runs_off_the_event_loop(monkeypatch):
    """cairosvg is CPU-bound — it goes through the thread pool like every sync call."""
    seen = []

    async def fake_pool(fn, *args):
        seen.append(fn)
        return fn(*args)

    monkeypatch.setattr(tgcharts, "run_in_threadpool", fake_pool)
    monkeypatch.setattr(charts, "to_png", lambda svg: PNG_MAGIC)
    assert await tgcharts._render("<svg/>") == PNG_MAGIC
    assert seen and seen[0] is charts.to_png


async def test_digest_text_survives_a_crashing_converter(session, monkeypatch):
    """The AC: a render failure must never lose the report — the digest text still goes."""
    from bot import jobs as jobs_module

    async def fake_digest(session, *, user_id, api_key):
        return "тиждень був добрий"

    async def crashing_chart(session, user_id):
        raise RuntimeError("cairo exploded")

    monkeypatch.setattr(jobs_module, "run_digest", fake_digest)
    monkeypatch.setattr(tgcharts, "digest_chart", crashing_chart)

    class Ctx:
        bot = _Bot()

    user = type("U", (), {"id": 1, "telegram_chat_id": 555})()
    creds = type("C", (), {"anthropic_key": "k"})()
    assert await jobs_module._deliver_digest(Ctx, session, user, creds) is True
    assert any("тиждень був добрий" in t for t in Ctx.bot.texts)
    assert Ctx.bot.photos == []


async def test_digest_sends_picture_after_text(session, monkeypatch):
    from bot import jobs as jobs_module

    async def fake_digest(session, *, user_id, api_key):
        return "текст"

    order = []

    class Bot(_Bot):
        async def send_message(self, chat_id, text, **kw):
            order.append("text")

        async def send_photo(self, chat_id, photo=None, caption=None, **kw):
            order.append("photo")

    async def fake_chart(session, user_id):
        return PNG_MAGIC

    monkeypatch.setattr(jobs_module, "run_digest", fake_digest)
    monkeypatch.setattr(tgcharts, "digest_chart", fake_chart)

    class Ctx:
        bot = Bot()

    user = type("U", (), {"id": 1, "telegram_chat_id": 555})()
    creds = type("C", (), {"anthropic_key": "k"})()
    await jobs_module._deliver_digest(Ctx, session, user, creds)
    assert order == ["text", "photo"]


async def test_weekly_km_between_buckets_from_window_start(session):
    from app.db.models import ActivityRecord
    from app.garmin import repository

    for aid, date, km in [(1, "2026-01-01", 5.0), (2, "2026-01-07", 6.0),
                          (3, "2026-01-08", 10.0), (4, "2026-01-20", 3.0),
                          (5, "2025-12-31", 99.0)]:
        session.add(ActivityRecord(user_id=777, activity_id=9_000_000 + aid, date=date,
                                   type="running", dist_km=km))
    await session.commit()
    out = await repository.weekly_km_between(session, 777, "2026-01-01", "2026-01-21")
    assert out == [11.0, 10.0, 3.0]
