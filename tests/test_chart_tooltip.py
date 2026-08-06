"""UI-01: the chart tooltip lives once, in ``app/static/app.js``, and formats right.

Two halves:

* the formatting rules are pure, so they're exercised under **node** — no browser, no
  page. This is what catches a merge losing one of the four dialects the old inline
  scripts had (``pace`` → ``4:52/км``, ``hr`` → ``152 уд``, ``f1`` → one decimal);
* a grep asserts no template grew its own copy back.

Both skip cleanly where node isn't installed (CI installs only ``.[dev]``)::

    ./venv/bin/python -m pytest tests/test_chart_tooltip.py
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APP_JS = ROOT / "app" / "static" / "app.js"
TEMPLATES = ROOT / "app" / "templates"

# app.js is written for a browser: it wires global listeners at load. A couple of stubs
# is all it takes to import it into node and reach the pure part.
_STUB = """
globalThis.window = {addEventListener: function () {}, chartTip: null};
globalThis.document = {
  readyState: 'complete',
  addEventListener: function () {},
  querySelectorAll: function () { return []; }
};
"""


def _node():
    return shutil.which("node") or shutil.which("nodejs")


def _run(expr):
    """Evaluate ``expr`` against the real app.js under node, returning parsed JSON."""
    node = _node()
    if not node:
        pytest.skip("node not installed")
    script = _STUB + APP_JS.read_text(encoding="utf-8") + f"\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_app_js_exposes_one_tooltip_formatter():
    assert _run("Object.keys(window.chartTip.formats).sort()") == [
        "cadence", "elev", "f1", "hr", "int", "pace", "power", "speed"
    ]


@pytest.mark.parametrize("fmt,value,expected", [
    # the dialects the four inline scripts used to hold separately
    ("pace", 4.8666666, "4:52/км"),
    ("pace", 5.999, "6:00/км"),        # the 59.99s → next minute rollover
    ("hr", 152.4, "152 уд"),
    ("f1", 7.24, "7.2"),
    ("int", 62.6, "63"),
    # the ones only detail.html knew about — the activity page used to label these "уд"
    ("speed", 31.44, "31.4 км/год"),
    ("power", 244.6, "245 Вт"),
    ("elev", 118.2, "118 м"),
    ("cadence", 172.5, "173 кр/хв"),
    # an unknown hint must not throw — it degrades to a plain number
    ("nonsense", 41.2, "41"),
])
def test_formats(fmt, value, expected):
    assert _run(f"window.chartTip.formats[{fmt!r}] "
                f"? window.chartTip.formats[{fmt!r}]({value}) "
                f": window.chartTip.text({fmt!r}, {{v: {value}}})") == expected


def test_tooltip_appends_the_point_label():
    # a trend point carries a date, an activity point a distance in km
    assert _run("window.chartTip.text('f1', {v: 7.2, lbl: '2026-08-01'})") == "7.2 · 2026-08-01"
    assert _run("window.chartTip.text('pace', {v: 5.0, d: 12.4})") == "5:00/км · 12.40 км"
    assert _run("window.chartTip.text('hr', {v: 140})") == "140 уд"


def test_no_template_carries_its_own_chart_script():
    offenders = [t.name for t in TEMPLATES.glob("*.html")
                 if "mousemove" in t.read_text(encoding="utf-8")]
    assert not offenders, (
        f"chart tooltip code copied back into: {offenders} — there is one implementation, "
        "in app/static/app.js"
    )
