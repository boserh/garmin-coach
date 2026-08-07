"""The registration → setup flow: the checklist, the Telegram deep link, and the
bot's ``/start``.

The bug this batch fixes was never a crash — it was that a new account had no idea what
to do next. So the tests are mostly about *what a user is told*: an unfinished account is
routed to the checklist (not to an empty dashboard or a raw settings form), the checklist
names the missing pieces, and the Telegram step is one tap instead of a copied chat id.

The page itself must also stay free: /onboarding is a pure DB read, and a checklist that
quietly cost a Claude call per view would be a bad trade for a nicer sentence.
"""
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace

import anyio
import pytest

from app import onboarding
from app.core import tglink
from app.core.crypto import hash_password
from app.db import users
from app.db.base import async_session_maker
from app.db.models import User
from tests.web_helpers import _seed_user

SECRET = "z" * 43 + "="   # shape of a Fernet key; only its bytes matter for signing


# --- the pure module ----------------------------------------------------------

def _steps(**over):
    flags = {"has_garmin": False, "has_anthropic": False, "has_telegram": False}
    flags.update(over)
    return onboarding.build_steps(**flags)


def test_fresh_account_owes_all_three_required_steps():
    steps = _steps()
    assert onboarding.progress(steps) == (0, 3)
    assert onboarding.is_complete(steps) is False
    assert onboarding.next_step(steps)["key"] == "garmin"
    assert onboarding.missing_labels(steps) == ["Garmin", "ключ Claude", "Telegram"]


def test_optional_plan_step_does_not_block_completion():
    # The plan is the payoff, not a credential — an account with all three creds is
    # configured whether or not it has generated a plan yet.
    steps = _steps(has_garmin=True, has_anthropic=True, has_telegram=True)
    assert onboarding.is_complete(steps) is True
    assert onboarding.progress(steps) == (3, 3)
    assert onboarding.missing_labels(steps) == []
    # ...and it's still offered as the next thing to do.
    assert onboarding.next_step(steps)["key"] == "plan"


def test_rejected_garmin_password_counts_as_unfinished():
    # Credentials are stored, so has_garmin is True — but Garmin refuses them and the
    # sync is stopped. A tick here would be a lie in the one state that needs action.
    steps = _steps(has_garmin=True, garmin_invalid=True,
                   has_anthropic=True, has_telegram=True)
    garmin = next(s for s in steps if s["key"] == "garmin")
    assert garmin["done"] is False
    assert garmin["note_level"] == "danger"
    assert onboarding.is_complete(steps) is False


def test_saved_creds_without_a_session_are_flagged_not_failed():
    steps = _steps(has_garmin=True, garmin_connected=False)
    garmin = next(s for s in steps if s["key"] == "garmin")
    assert garmin["done"] is True          # nothing to fix, just not proven yet
    assert garmin["note_level"] == "warn"

    connected = next(s for s in _steps(has_garmin=True, garmin_connected=True)
                     if s["key"] == "garmin")
    assert connected["note_level"] == "ok"


def test_telegram_step_falls_back_to_manual_chat_id_without_a_link():
    with_link = next(s for s in _steps(telegram_link="https://t.me/b?start=x")
                     if s["key"] == "telegram")
    assert with_link["action"].startswith("https://t.me/")

    without = next(s for s in _steps() if s["key"] == "telegram")
    assert without["action"] == "/settings#telegram"
    assert any("@userinfobot" in line for line in without["how"])


def test_done_steps_offer_a_change_action_not_a_setup_one():
    # A finished Telegram step must point at the settings field, NOT back at the deep
    # link — "змінити" that re-runs the linking flow reads as a broken tick.
    step = next(s for s in _steps(has_telegram=True, telegram_link="https://t.me/b?start=x")
                if s["key"] == "telegram")
    assert step["action"] == "/settings#telegram"


# --- the link token -----------------------------------------------------------

def test_token_round_trip(monkeypatch):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    assert tglink.parse_token(tglink.make_token(42)) == 42


def test_tampered_and_expired_tokens_are_rejected(monkeypatch):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    token = tglink.make_token(7)
    assert tglink.parse_token(token + "x") is None
    assert tglink.parse_token("") is None
    # signed for user 7, but read a moment too late
    assert tglink.parse_token(token, max_age=-1) is None


def test_token_signed_with_another_key_is_rejected(monkeypatch):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    token = tglink.make_token(7)
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", "a" * 43 + "=")
    assert tglink.parse_token(token) is None


def test_link_unavailable_without_a_shared_secret(monkeypatch):
    # Bot and web must agree on APP_SECRET_KEY for the token to verify across
    # processes. Without it we offer no button rather than a link that can't work.
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", "")
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", "somebot")
    assert tglink.available() is False
    assert tglink.deep_link(1) is None
    assert tglink.parse_token("anything") is None

    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", None)
    assert tglink.deep_link(1) is None


def test_token_obeys_telegrams_start_parameter_rules(monkeypatch):
    """Telegram documents a ?start= payload as at most 64 characters of A-Z a-z 0-9 _ -.
    The first cut used itsdangerous, whose tokens are joined with DOTS
    ("MQ.anZMaQ._Epkc…") — url-safe in general, outside Telegram's set here, so the
    payload a client passed on was anyone's guess. Nothing in the app could notice: the
    link rendered, the button looked fine, and the tap simply did not link the account.
    """
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    # Across the whole id range the column can hold, not just the id this install has.
    for user_id in (1, 42, 999_999, 2**31 - 1):
        token = tglink.make_token(user_id)
        assert re.fullmatch(r"[A-Za-z0-9_-]+", token), f"uid={user_id}: {token!r}"
        assert len(token) <= tglink.START_PARAM_MAX, f"uid={user_id}: {len(token)} chars"
        assert tglink.parse_token(token) == user_id


def test_deep_link_points_at_the_bot_with_a_start_payload(monkeypatch):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", "coach_bot")
    url = tglink.deep_link(3)
    assert url.startswith("https://t.me/coach_bot?start=")
    assert tglink.parse_token(url.split("start=")[1]) == 3


# --- the page -----------------------------------------------------------------

EMAIL = "onboard@example.com"


@pytest.fixture
def page_client(client):
    # The web fixtures share one SQLite file across the module, so each test starts by
    # resetting this account back to "nothing configured" — otherwise a test that ticks
    # a step off silently configures the ones that run after it.
    _seed_user(email=EMAIL, password="pw", is_admin=False)
    _configure(EMAIL, garmin_email_enc=None, garmin_password_enc=None,
               garth_token_enc=None, anthropic_key_enc=None, telegram_chat_id=None,
               garmin_creds_invalid=False)
    client.post("/login", data={"email": EMAIL, "password": "pw"})
    return client


@pytest.fixture
def no_llm_no_garmin(monkeypatch):
    """Anything reaching for Claude or Garmin from this request fails loudly."""
    import app.analysis.client as client_mod
    import app.garmin.providers as providers

    def boom(*a, **kw):
        raise AssertionError("the onboarding page must not call out to Claude/Garmin")

    monkeypatch.setattr(client_mod, "_get_client", boom, raising=False)
    monkeypatch.setattr(providers, "get_provider", boom, raising=False)


def _configure(email, **fields):
    async def go():
        async with async_session_maker() as s:
            u = await users.get_by_email(s, email)
            for k, v in fields.items():
                setattr(u, k, v)
            await s.commit()

    anyio.run(go)


def test_onboarding_requires_login(client):
    r = client.get("/onboarding", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_onboarding_lists_every_step_for_a_fresh_account(page_client, no_llm_no_garmin):
    r = page_client.get("/onboarding")
    assert r.status_code == 200
    for title in ("Підключити Garmin Connect", "Додати ключ Claude API",
                  "Підключити Telegram-бота", "Створити програму тренувань"):
        assert title in r.text
    assert "Залишилось 3 з 3" in r.text


def test_onboarding_ticks_off_what_is_done(page_client, no_llm_no_garmin):
    _configure(EMAIL, garmin_email_enc="x", garmin_password_enc="x", anthropic_key_enc="x")
    r = page_client.get("/onboarding")
    assert "Залишилось 1 з 3" in r.text
    assert r.text.count('class="obstep done"') == 2


def test_onboarding_congratulates_a_configured_account(page_client, no_llm_no_garmin):
    _configure(EMAIL, garmin_email_enc="x", garmin_password_enc="x",
               anthropic_key_enc="x", telegram_chat_id=987001)
    r = page_client.get("/onboarding")
    assert "Все підключено" in r.text
    assert "Залишилось" not in r.text


def test_onboarding_offers_the_one_tap_link_when_configured(page_client, monkeypatch,
                                                            no_llm_no_garmin):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", "coach_bot")
    r = page_client.get("/onboarding")
    assert "https://t.me/coach_bot?start=" in r.text
    assert "@userinfobot" not in r.text


def test_onboarding_falls_back_to_chat_id_instructions(page_client, monkeypatch,
                                                       no_llm_no_garmin):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", "")
    r = page_client.get("/onboarding")
    assert "@userinfobot" in r.text
    assert "?start=" not in r.text


def test_settings_shows_the_connect_button_instead_of_the_manual_hunt(page_client,
                                                                     monkeypatch):
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", "coach_bot")
    r = page_client.get("/settings")
    assert "Підключити Telegram" in r.text
    assert "https://t.me/coach_bot?start=" in r.text


def test_nav_carries_the_checklist_only_while_it_is_unfinished(page_client,
                                                               no_llm_no_garmin):
    assert '/onboarding' in page_client.get("/dashboard").text

    _configure(EMAIL, garmin_email_enc="x", garmin_password_enc="x",
               anthropic_key_enc="x", telegram_chat_id=987002)
    assert '/onboarding' not in page_client.get("/dashboard").text


def test_dashboard_banner_names_what_is_missing(page_client, no_llm_no_garmin):
    _configure(EMAIL, garmin_email_enc="x", garmin_password_enc="x")
    body = page_client.get("/dashboard").text
    assert "Налаштування не завершено" in body
    assert "ключ Claude" in body and "Telegram" in body
    # the generic "no history yet" note is the symptom, not the cause — it stays off
    # until setup is done, so the page carries one instruction, not two.
    assert "Ще немає історії" not in body


def test_registration_explains_what_happens_next(client):
    r = client.post(
        "/register", data={"email": "justregistered@example.com", "password": "secret1"},
        follow_redirects=False)
    assert r.status_code == 200
    assert "justregistered@example.com" in r.text
    assert "підтвердить" in r.text          # the approval wait
    assert "Garmin Connect" in r.text       # ...and the three steps that follow
    assert "ключ Claude API" in r.text
    assert "Telegram-бота" in r.text


# --- the bot's /start ---------------------------------------------------------

class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(chat_id, msg):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id),
        message=msg,
        effective_message=msg,
        callback_query=None,
    )


@pytest.fixture
def bot_session(session, monkeypatch):
    import bot.handlers as handlers

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(handlers, "async_session_maker", maker)
    monkeypatch.setattr(tglink.settings, "APP_SECRET_KEY", SECRET)
    monkeypatch.setattr(tglink.settings, "TELEGRAM_BOT_USERNAME", "coach_bot")
    return session


async def _mk_user(session, email, **fields):
    user = User(email=email, password_hash=hash_password("pw"),
                is_approved=True, is_active=True, **fields)
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def test_start_with_a_token_links_the_chat(bot_session):
    import bot.handlers as handlers

    user = await _mk_user(bot_session, "link@example.com",
                          garmin_email_enc="x", garmin_password_enc="x",
                          anthropic_key_enc="x")
    msg = _FakeMessage()
    ctx = SimpleNamespace(args=[tglink.make_token(user.id)])
    await handlers.start_cmd(_update(555, msg), ctx)

    await bot_session.refresh(user)
    assert user.telegram_chat_id == 555
    assert "link@example.com" in msg.replies[0]
    assert "Все налаштовано" in msg.replies[0]   # nothing left → say so


async def test_start_reply_names_what_is_still_missing(bot_session):
    import bot.handlers as handlers

    user = await _mk_user(bot_session, "half@example.com",
                          garmin_email_enc="x", garmin_password_enc="x")
    msg = _FakeMessage()
    await handlers.start_cmd(_update(556, msg),
                             SimpleNamespace(args=[tglink.make_token(user.id)]))

    await bot_session.refresh(user)
    assert user.telegram_chat_id == 556
    assert "ключ Claude" in msg.replies[0]


async def test_start_with_a_bad_token_links_nothing(bot_session):
    import bot.handlers as handlers

    user = await _mk_user(bot_session, "nope@example.com")
    msg = _FakeMessage()
    await handlers.start_cmd(_update(557, msg), SimpleNamespace(args=["garbage"]))

    await bot_session.refresh(user)
    assert user.telegram_chat_id is None
    assert "недійсне" in msg.replies[0]


async def test_start_refuses_an_unapproved_account(bot_session):
    import bot.handlers as handlers

    user = await _mk_user(bot_session, "pending@example.com")
    user.is_approved = False
    await bot_session.commit()

    msg = _FakeMessage()
    await handlers.start_cmd(_update(558, msg),
                             SimpleNamespace(args=[tglink.make_token(user.id)]))
    await bot_session.refresh(user)
    assert user.telegram_chat_id is None
    assert "не підтверджено" in msg.replies[0]


async def test_start_hands_a_chat_over_instead_of_hitting_the_unique_constraint(bot_session):
    # telegram_chat_id is UNIQUE. Re-linking the same phone to a second account is a
    # normal thing to do, and both halves are proved: the token proves the web account,
    # the incoming update proves the chat.
    import bot.handlers as handlers

    old = await _mk_user(bot_session, "old@example.com", telegram_chat_id=559)
    new = await _mk_user(bot_session, "new@example.com")

    msg = _FakeMessage()
    await handlers.start_cmd(_update(559, msg),
                             SimpleNamespace(args=[tglink.make_token(new.id)]))

    await bot_session.refresh(old)
    await bot_session.refresh(new)
    assert old.telegram_chat_id is None
    assert new.telegram_chat_id == 559


async def test_bare_start_from_an_unknown_chat_explains_the_order(bot_session):
    import bot.handlers as handlers

    msg = _FakeMessage()
    await handlers.start_cmd(_update(560, msg), SimpleNamespace(args=[]))
    assert "зареєструйся" in msg.replies[0].lower()
    assert "Підключення" in msg.replies[0]


async def test_bare_start_from_a_linked_chat_reports_the_state(bot_session):
    import bot.handlers as handlers

    await _mk_user(bot_session, "known@example.com", telegram_chat_id=561,
                   garmin_email_enc="x", garmin_password_enc="x")
    msg = _FakeMessage()
    await handlers.start_cmd(_update(561, msg), SimpleNamespace(args=[]))
    assert "known@example.com" in msg.replies[0]
    assert "ключ Claude" in msg.replies[0]      # still owed


def test_start_is_wired_on_the_product_bot():
    from telegram.ext import CommandHandler

    import bot.main as bot_main

    class _FakeApp:
        def __init__(self):
            self.handlers = []

        def add_handler(self, handler, group=0):
            self.handlers.append(handler)

        def add_error_handler(self, handler):
            pass

    app = _FakeApp()
    bot_main.register_handlers(app)
    names = {c for h in app.handlers if isinstance(h, CommandHandler) for c in h.commands}
    assert "start" in names


def test_demo_account_is_sent_past_the_checklist(client):
    # It has nothing to configure and every write path short-circuits — a checklist it
    # could never finish would be a dead end, not help.
    client.post("/demo-login")
    r = client.get("/onboarding", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/dashboard"
    assert "/onboarding" not in client.get("/dashboard").text
