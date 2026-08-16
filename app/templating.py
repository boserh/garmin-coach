"""One Jinja environment for every router (UI-02).

Each router used to build its own ``Jinja2Templates`` and every template repeated the
same ``<head>`` verbatim — including a hand-bumped ``app.css?v=4``, which had already
drifted out of sync with the mobile-layout guard's string replacement. So the version
is derived here from the asset bytes instead of typed by hand, and pages inherit
``_base.html`` rather than copying its head.

``asset_v`` is computed once at import (process start): the Pi restarts its services on
deploy, so a fresh version lands with every changed file, and no request pays a stat().
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"

# The assets whose contents the ``?v=`` query string stands for. Fonts and icons are
# immutable in practice and are left out so a font refresh doesn't churn every URL.
_VERSIONED = ("app.css", "app.js", "sw.js")


def asset_version() -> str:
    """A short digest of the mutable static assets — the cache-busting ``?v=`` value."""
    h = hashlib.sha256()
    for name in _VERSIONED:
        try:
            st = (STATIC_DIR / name).stat()
        except OSError:
            # A missing asset is not fatal (sw.js is optional in some checkouts); it
            # simply contributes nothing to the digest.
            continue
        h.update(f"{name}:{st.st_mtime_ns}:{st.st_size}".encode())
    return h.hexdigest()[:10]


ASSET_V = asset_version()


def create_templates() -> Jinja2Templates:
    """A ``Jinja2Templates`` bound to the shared directory, with the globals every
    page's ``<head>`` needs. Routers add their own filters on top."""
    from app import away as away_rules

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.globals["asset_v"] = ASSET_V
    # NF-34: one rendering of an away period ("🪁 інший спорт · 16.08-24.08 · кайт"), shared
    # by the chat's proposal card and the profile page — the same string the bot sends, so
    # the three surfaces can't describe the same trip three ways.
    templates.env.filters["away_line"] = away_rules.describe
    return templates
