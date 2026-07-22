"""ST-12: /costs command — pure month-parsing/formatting helpers."""
import datetime as dt
from zoneinfo import ZoneInfo

from bot.handlers import _format_costs, _month_bounds_utc, _parse_month_arg

WARSAW = ZoneInfo("Europe/Warsaw")


def test_parse_month_arg_defaults_to_current_month():
    now = dt.datetime.now(WARSAW)
    assert _parse_month_arg(None, WARSAW) == (now.year, now.month)
    assert _parse_month_arg("", WARSAW) == (now.year, now.month)


def test_parse_month_arg_parses_explicit_month():
    assert _parse_month_arg("2026-06", WARSAW) == (2026, 6)


def test_parse_month_arg_rejects_garbage():
    assert _parse_month_arg("garbage", WARSAW) is None
    assert _parse_month_arg("2026-13", WARSAW) is None


def test_month_bounds_utc_spans_the_calendar_month():
    start, end = _month_bounds_utc(2026, 6, WARSAW)
    assert start.astimezone(WARSAW) == dt.datetime(2026, 6, 1, tzinfo=WARSAW)
    assert end.astimezone(WARSAW) == dt.datetime(2026, 7, 1, tzinfo=WARSAW)
    assert start.tzinfo == dt.timezone.utc


def test_month_bounds_utc_rolls_over_december():
    start, end = _month_bounds_utc(2026, 12, WARSAW)
    assert end.astimezone(WARSAW) == dt.datetime(2027, 1, 1, tzinfo=WARSAW)


def test_format_costs_empty_month():
    agg = {"total_usd": 0.0, "calls": 0, "cached": 0, "by_kind": {}, "top3": []}
    text = _format_costs(agg, 2026, 6)
    assert "викликів не було" in text
    assert "2026-06" in text


def test_format_costs_breaks_down_by_kind_and_top3():
    agg = {
        "total_usd": 0.08, "calls": 4, "cached": 1,
        "by_kind": {"report": {"cost": 0.03, "calls": 3}, "deep": {"cost": 0.05, "calls": 1}},
        "top3": [{"kind": "deep", "date": "2026-06-01", "cost": 0.05}],
    }
    text = _format_costs(agg, 2026, 6)
    assert "$0.08" in text
    assert "Викликів: 4 (з кешу: 1)" in text
    assert "глибокий аналіз" in text
    assert "щоденний звіт" in text
    assert "2026-06-01" in text
