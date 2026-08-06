"""Refresh the self-hosted Inter subsets in ``app/static/fonts/`` (UI-02).

The app must render identically without internet access, so the webfont ships with the
repo instead of coming from a CDN at first paint. Google serves Inter as ONE variable
woff2 per subset (the per-weight URLs are byte identical), so we keep one file per
subset and declare ``font-weight: 400 700`` over it in ``app.css``.

Run this only when Inter needs updating — it needs network, prints the ``@font-face``
block to paste into ``app.css``, and is not part of the app or the test suite::

    ./venv/bin/python scripts/fetch_inter.py
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

# Google returns different (legacy) formats to unknown user agents; ask as a browser.
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
URL = "https://fonts.googleapis.com/css2?family=Inter:wght@400..700&display=swap"
# Ukrainian needs `cyrillic` (і/ї/є live in U+0400-045F, ґ in U+0490-0491); the rest of
# the world's subsets (greek, vietnamese, …) would be dead weight here.
WANT = ["latin", "latin-ext", "cyrillic", "cyrillic-ext"]
OUT = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "fonts"


def main() -> None:
    css = subprocess.run(
        ["curl", "-sS", "-m", "60", "-H", f"User-Agent: {UA}", URL],
        capture_output=True, text=True, check=True,
    ).stdout

    faces: dict[str, tuple[str, str]] = {}
    for subset, body in re.findall(r"/\* (\S+) \*/\s*@font-face \{(.*?)\}", css, re.S):
        if subset in WANT and subset not in faces:
            faces[subset] = (
                re.search(r"url\((\S+?)\)", body).group(1),
                re.search(r"unicode-range:\s*([^;]+);", body).group(1).strip(),
            )
    missing = [s for s in WANT if s not in faces]
    if missing:
        raise SystemExit(f"Google Fonts returned no @font-face for: {', '.join(missing)}")

    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    for subset in WANT:
        url, rng = faces[subset]
        name = f"inter-{subset}.woff2"
        subprocess.run(["curl", "-sS", "-m", "60", "-o", str(OUT / name), url], check=True)
        print(
            f"@font-face{{font-family:'Inter';font-style:normal;font-weight:400 700;"
            f"font-display:swap;src:url('/static/fonts/{name}') format('woff2');"
            f"unicode-range:{rng}}}"
        )


if __name__ == "__main__":
    main()
