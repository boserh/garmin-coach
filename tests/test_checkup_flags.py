"""`is_out_of_range`: best-effort out-of-range flag for the checkups results table —
handles a plain range (either order), a `<`/`>`/`<=`/`>=` bound, and European decimal
commas; anything else (missing/unparseable) stays `None` (no opinion, no highlight)."""
import pytest

from app.checkup_flags import is_out_of_range


@pytest.mark.parametrize("value, ref_range, expected", [
    ("45", "30-400", False),
    ("15", "30-400", True),
    ("500", "30-400", True),
    ("45", "400-30", False),         # reversed range, still handled
    ("3.2", "3,5-5,5", True),        # comma-decimal ref_range
    ("4,0", "3.5-5.5", False),       # comma-decimal value
    ("6", "<5.0", True),
    ("4.9", "<5.0", False),
    ("5", "<=5", False),
    ("5.1", "<=5", True),
    ("3", ">10", True),
    ("11", ">10", False),
    ("10", ">=10", False),
    ("9.9", ">=10", True),
    ("45 (high)", "30-400", False),  # leading number extracted from prose
])
def test_is_out_of_range_recognized_shapes(value, ref_range, expected):
    assert is_out_of_range(value, ref_range) is expected


@pytest.mark.parametrize("value, ref_range", [
    (None, "30-400"),
    ("45", None),
    ("", "30-400"),
    ("45", ""),
    ("positive", "negative"),        # non-numeric value
    ("45", "normal"),                # prose ref_range, no recognizable bound
    ("45", "30 to 400"),             # unrecognized range spelling
])
def test_is_out_of_range_unrecognized_returns_none(value, ref_range):
    assert is_out_of_range(value, ref_range) is None
