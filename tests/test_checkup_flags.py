"""`out_of_range_severity`: best-effort out-of-range flag for the checkups results
table, graded by how far past the boundary the value falls — a plain range (either
order), a `<`/`>`/`<=`/`>=` bound, and European decimal commas; anything else
(missing/unparseable) stays `None` (no opinion, no highlight)."""
import pytest

from app.checkup_flags import out_of_range_severity


@pytest.mark.parametrize("value, ref_range, expected", [
    ("15", "10-20", None),        # within range
    ("9", "10-20", "minor"),      # 1 below lo, range width 10 -> 10% past the edge
    ("8", "10-20", "major"),      # 2 below lo -> 20% past the edge
    ("21", "10-20", "minor"),     # 1 above hi
    ("25", "10-20", "major"),     # 5 above hi -> 50% past the edge
    ("45", "400-30", None),       # reversed range, still handled (value in range)
    ("4.9", "<5.0", None),
    ("5.5", "<5.0", "minor"),     # bound 5.0, 0.5 past -> 10%
    ("6", "<5.0", "major"),       # 1 past -> 20%
    ("5", "<=5", None),           # boundary itself is still "in range"
    ("5.5", "<=5", "minor"),
    ("11", ">10", None),
    ("9", ">10", "minor"),
    ("5", ">10", "major"),
    ("6", "5-5", "major"),        # zero-width range can't express a fraction -> major
    ("0.1", "<0", "major"),       # zero bound, same reasoning
    ("45 (high)", "30-400", None),  # leading number extracted from prose, in range
])
def test_out_of_range_severity_recognized_shapes(value, ref_range, expected):
    assert out_of_range_severity(value, ref_range) == expected


@pytest.mark.parametrize("value, ref_range", [
    (None, "30-400"),
    ("45", None),
    ("", "30-400"),
    ("45", ""),
    ("positive", "negative"),        # non-numeric value
    ("45", "normal"),                # prose ref_range, no recognizable bound
    ("45", "30 to 400"),             # unrecognized range spelling
])
def test_out_of_range_severity_unrecognized_returns_none(value, ref_range):
    assert out_of_range_severity(value, ref_range) is None
