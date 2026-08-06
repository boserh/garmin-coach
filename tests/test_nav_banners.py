"""UI-07: one banner component, one section list, and a thumb-reachable tab bar.

The structural half (no hexes in templates, right ARIA role, tab bar only where it
belongs) needs no browser. The geometry half — "the nav is at most one line tall and
the bar doesn't cover the content" — lives in ``tests/test_nav_layout.py``.
"""
from pathlib import Path

import pytest

from app.banners import banner

TEMPLATES = Path(__file__).resolve().parent.parent / "app" / "templates"


def test_no_template_hand_styles_a_banner():
    offenders = []
    for t in TEMPLATES.glob("*.html"):
        text = t.read_text(encoding="utf-8")
        if 'style="border-color:#' in text or 'style="border-left:3px solid #' in text:
            offenders.append(t.name)
    assert not offenders, (
        f"inline banner colours re-added in: {offenders} — use .banner + a level, so the "
        "colour comes from the design tokens"
    )


def test_banner_rejects_an_unknown_level():
    # A typo'd level would otherwise render as an unstyled div AND lose its ARIA role.
    with pytest.raises(ValueError):
        banner("scary", "боляче")


@pytest.mark.parametrize("level,role", [
    ("info", "status"), ("ok", "status"), ("muted", "status"),
    ("warn", "alert"), ("danger", "alert"),
])
def test_the_level_decides_the_aria_role(auth_client, level, role, monkeypatch):
    from app.routers import dashboard as dash

    monkeypatch.setattr(dash, "_dashboard_banners",
                        lambda *a, **k: [banner(level, "текст", icon="!")])
    html = auth_client.get("/dashboard").text
    assert f'class="banner banner--{level}"' in html
    assert f'role="{role}"' in html


def test_a_banner_link_renders_as_a_trailing_action(auth_client, monkeypatch):
    from app.routers import dashboard as dash

    monkeypatch.setattr(dash, "_dashboard_banners", lambda *a, **k: [
        banner("warn", "Бекап БД застарів.", icon="⚠️", link="/status",
               link_text="Статус →")])
    html = auth_client.get("/dashboard").text
    assert "Бекап БД застарів." in html
    assert '<a class="blink" href="/status">Статус →</a>' in html


def test_dashboard_banners_are_built_from_data():
    from types import SimpleNamespace

    from app.routers.dashboard import _dashboard_banners

    clean = SimpleNamespace(garmin_creds_invalid=False, is_admin=False)
    assert _dashboard_banners(clean, garmin_errors=None, backup=None, backup_warn_days=3,
                              llm_budget=None, has_history=True) == []

    broken = SimpleNamespace(garmin_creds_invalid=True, is_admin=False)
    out = _dashboard_banners(broken, garmin_errors={"count_24h": 4, "counts_24h": {"403": 4}},
                             backup={"age_hours": None, "rsync_ok": None},
                             backup_warn_days=3,
                             llm_budget={"warn": True, "blocked": False,
                                         "soft_blocked": True, "month_usd": 22.5,
                                         "month_limit": 25.0, "pct": 90.0,
                                         "projected_month_usd": 27.1},
                             has_history=False)
    assert [b["level"] for b in out] == ["danger", "warn", "warn", "warn", "info"]
    assert "403×4" in out[1]["text"]
    assert "бекапи ще не налаштовані" in out[2]["text"]
    assert "Фонові звіти призупинені" in out[3]["text"]


def test_a_spent_budget_reads_as_broken_not_merely_worrying():
    from types import SimpleNamespace

    from app.routers.dashboard import _dashboard_banners

    out = _dashboard_banners(
        SimpleNamespace(garmin_creds_invalid=False, is_admin=False),
        garmin_errors=None, backup=None, backup_warn_days=3,
        llm_budget={"warn": True, "blocked": True, "soft_blocked": True,
                    "month_usd": 25.4, "month_limit": 25.0, "pct": 101.6,
                    "projected_month_usd": 30.0},
        has_history=True)
    assert [b["level"] for b in out] == ["danger"]
    assert "зупинені" in out[0]["text"]


def test_activity_banners_separate_the_action_from_the_state():
    from app.routers.me import _activity_banners

    # Just hidden: the "you did this" note, not the standing one as well.
    just_hidden = _activity_banners(resynced=False, regen="", hidden=True, shown=False,
                                    is_hidden=True)
    assert len(just_hidden) == 1 and just_hidden[0]["level"] == "warn"

    # Opened later: only the standing note.
    later = _activity_banners(resynced=False, regen="", hidden=False, shown=False,
                              is_hidden=True)
    assert [b["level"] for b in later] == ["muted"]

    # An unknown regen value is simply not a banner (a stray query param can't inject one).
    assert _activity_banners(resynced=False, regen="haha", hidden=False, shown=False,
                             is_hidden=False) == []

    nokey = _activity_banners(resynced=False, regen="nokey", hidden=False, shown=False,
                              is_hidden=False)
    assert nokey[0]["link"] == "/settings"


def test_the_tab_bar_is_on_the_app_pages(auth_client):
    html = auth_client.get("/dashboard").text
    assert 'class="tabbar"' in html
    # Main sections in the bar, the rest behind "Ще" — and the current one marked for
    # assistive tech, not by colour alone.
    for label in ("Дашборд", "Програма", "Мої дані", "Чат"):
        assert label in html
    assert 'aria-current="page"' in html
    assert 'class="tabmore"' in html


def test_the_tab_bar_stays_off_the_auth_pages(client):
    # There is nowhere to navigate to before logging in, and the bar would only cover
    # the form on a phone.
    for url in ("/login", "/register"):
        html = client.get(url).text
        assert 'class="tabbar"' not in html, url
        assert 'class="topnav"' not in html, url


def test_admin_only_sections_stay_admin_only(client):
    from tests.web_helpers import _seed_user

    _seed_user(email="plain-nav@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "plain-nav@example.com", "password": "pw"})
    html = client.get("/settings").text
    assert "База даних" not in html and "Кеші" not in html
