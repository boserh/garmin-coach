"""Render the PWA's PNG icons from ``app/static/icon.svg`` (UI-03).

Chrome refuses to offer an install prompt without raster icons at 192 and 512, plus a
``maskable`` variant so Android doesn't letterbox the logo inside its adaptive shape.
The SVG stays the source of truth; these are generated from it.

Uses the Chromium that ships with Playwright (already a dev dependency for the layout
guard) rather than adding a rasteriser. Run only when the logo changes::

    ./venv/bin/python scripts/render_icons.py
"""
from __future__ import annotations

import pathlib
import shutil

STATIC = pathlib.Path(__file__).resolve().parent.parent / "app" / "static"
SVG = STATIC / "icon.svg"

# (filename, pixel size, safe-zone padding). Maskable icons are cropped to a circle by
# Android, so the artwork is inset to ~80% — the rest is bleed the launcher may eat.
TARGETS = [
    ("icon-192.png", 192, 0.0),
    ("icon-512.png", 512, 0.0),
    ("icon-maskable-512.png", 512, 0.1),
]


def _chromium() -> str | None:
    import os

    for cand in ("/opt/pw-browsers/chromium", shutil.which("chromium"),
                 shutil.which("chromium-browser"), shutil.which("google-chrome")):
        if cand and os.path.exists(cand):
            return cand
    return None


def main() -> None:
    from playwright.sync_api import sync_playwright

    exe = _chromium()
    if not exe:
        raise SystemExit("no Chromium binary found — install one or set PLAYWRIGHT_BROWSERS_PATH")
    svg = SVG.read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=exe)
        try:
            for name, size, pad in TARGETS:
                inset = round(size * pad)
                page = browser.new_page(viewport={"width": size, "height": size},
                                        device_scale_factor=1)
                page.set_content(
                    "<style>html,body{margin:0;background:#090b0f}"
                    f"svg{{display:block;width:{size - 2 * inset}px;"
                    f"height:{size - 2 * inset}px;margin:{inset}px}}</style>" + svg
                )
                page.screenshot(path=str(STATIC / name), omit_background=False)
                page.close()
                print(f"wrote {name} ({size}px, pad {int(pad * 100)}%)")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
