"""Page-level notices, built as data (UI-07).

The dashboard carried five of these and the activity page eight, each hand-styled with
an inline ``style="border-color:#f7768e;background:rgba(247,118,142,.08)"`` — the same
warning amber typed out in three places, the same ``rgba(…, .08)`` formula in four.
Adding a state meant copying a hex.

Now a router builds a list of :func:`banner` dicts and ``_banners.html`` renders it;
the colour comes from a level, the level also picks the ARIA role. The "show it or
not" logic stays in the router, where the data it depends on already is.
"""
from __future__ import annotations

# info: neutral context · ok: something succeeded · warn: needs attention ·
# danger: something is broken or stopped · muted: a quiet state note.
LEVELS = ("info", "ok", "warn", "danger", "muted")


def banner(level: str, text: str, *, icon: str = "", link: str = "",
           link_text: str = "") -> dict:
    """One notice. ``link``/``link_text`` render as a trailing action, so the sentence
    reads the same whether or not there's somewhere to go."""
    if level not in LEVELS:
        raise ValueError(f"unknown banner level: {level!r} (expected one of {LEVELS})")
    return {"level": level, "text": text, "icon": icon, "link": link,
            "link_text": link_text}
