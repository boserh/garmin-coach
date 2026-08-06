"""UI-02: one ``<head>``, one asset version, no third-party font.

The head used to be copy-pasted into 26 templates, which is how ``app.css?v=`` drifted
out of sync with the mobile-layout guard and how ``activity.html`` ended up without a
manifest. These are cheap structural assertions that keep it that way.
"""
import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"
STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

PAGES = [t for t in sorted(TEMPLATES.glob("*.html")) if t.name != "_base.html"]


def test_only_the_base_template_owns_the_document_shell():
    offenders = [t.name for t in PAGES if "<!doctype" in t.read_text(encoding="utf-8").lower()]
    assert not offenders, f"these still carry their own <!doctype>: {offenders}"
    assert "<!doctype" in (TEMPLATES / "_base.html").read_text(encoding="utf-8").lower()


def test_no_template_pulls_a_font_from_a_third_party():
    offenders = [t.name for t in TEMPLATES.glob("*.html")
                 if "fonts.googleapis.com" in t.read_text(encoding="utf-8")
                 or "fonts.gstatic.com" in t.read_text(encoding="utf-8")]
    assert not offenders, f"external font requests re-added in: {offenders}"


def test_stylesheet_declares_the_self_hosted_font():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    faces = re.findall(r"src:url\('(/static/fonts/[^']+)'\)", css)
    assert faces, "app.css no longer @font-face's the local Inter"
    for ref in faces:
        assert (STATIC / ref[len("/static/"):]).exists(), f"missing font file: {ref}"
    # Ukrainian-specific letters must be inside a declared unicode-range, or the page
    # silently falls back to a system font mid-word.
    ranges = " ".join(re.findall(r"unicode-range:([^}]+)}", css))
    assert "U+0400-045F" in ranges and "U+0490-0491" in ranges


@pytest.mark.parametrize("url", ["/dashboard", "/plan", "/chat", "/settings", "/me"])
def test_every_page_gets_the_shared_head(auth_client, url):
    html = auth_client.get(url).text
    assert '<link rel="manifest" href="/static/manifest.json">' in html
    assert '<meta name="theme-color" content="#090b0f">' in html
    assert '<link rel="icon" href="/static/icon.svg" type="image/svg+xml">' in html
    assert "fonts.googleapis.com" not in html


def test_asset_version_follows_the_asset_bytes(auth_client, tmp_path, monkeypatch):
    from app import templating

    html = auth_client.get("/dashboard").text
    v = re.search(r"/static/app\.css\?v=([0-9a-f]+)", html).group(1)
    assert v == templating.ASSET_V
    assert f"/static/app.js?v={v}" in html

    # Touching the stylesheet must produce a different version with no template edit —
    # the manual "?v=4" bump is the class of mistake this replaces.
    fake = tmp_path / "static"
    fake.mkdir()
    (fake / "app.css").write_text("a{}")
    monkeypatch.setattr(templating, "STATIC_DIR", fake)
    first = templating.asset_version()
    (fake / "app.css").write_text("a{color:red}")
    assert templating.asset_version() != first
