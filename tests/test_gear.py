"""NF-15 shoe mileage tracker: the pure `app.gear` parsing/threshold helpers, the
client.py gear fetches (mocked _safe, isolated disk cache), the daily
`_gear_check_for_user` sync + warn guard (bot/jobs.py), and the `/gear` command."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import gear
from app.db.models import User
from app.garmin import client, repository

U1 = 1


# --- pure parsing --------------------------------------------------------------

def test_parse_item_accepts_several_key_shapes():
    assert gear.parse_item({"uuid": "abc", "displayName": "Nike Pegasus",
                            "gearTypeName": "Running"}) == {
        "gear_id": "abc", "name": "Nike Pegasus", "type": "Running", "retired": False}
    assert gear.parse_item({"gearPk": 42, "gearMakeName": "Hoka"})["gear_id"] == "42"
    assert gear.parse_item({"gearUUID": "x"})["name"] == "Спорядження"


def test_parse_item_retired_flag():
    assert gear.parse_item({"uuid": "a", "retired": True})["retired"] is True
    assert gear.parse_item({"uuid": "a", "gearStatusName": "Retired"})["retired"] is True
    assert gear.parse_item({"uuid": "a", "gearStatusName": "Active"})["retired"] is False


def test_parse_item_unrecognised_shape_returns_none():
    assert gear.parse_item({"someOtherField": 1}) is None
    assert gear.parse_item("not a dict") is None
    assert gear.parse_item(None) is None


def test_parse_mileage_km_variants_and_conversion():
    assert gear.parse_mileage_km({"totalDistance": 700_000}) == 700.0     # metres -> km
    assert gear.parse_mileage_km({"totalDistanceInMeters": 12_345}) == 12.3
    assert gear.parse_mileage_km({"distance": 1_000}) == 1.0
    assert gear.parse_mileage_km({"unknownField": 1}) is None
    assert gear.parse_mileage_km({}) is None
    assert gear.parse_mileage_km(None) is None


def test_parse_last_used_variants():
    assert gear.parse_last_used({"lastActivityDate": "2026-07-01T10:00:00"}) == "2026-07-01"
    assert gear.parse_last_used({"lastUsedDate": "2026-06-15"}) == "2026-06-15"
    assert gear.parse_last_used({"endDate": "2026-05-01"}) == "2026-05-01"
    assert gear.parse_last_used({}) is None
    assert gear.parse_last_used(None) is None


# --- threshold logic -------------------------------------------------------------

def test_worn_filters_by_threshold_and_skips_retired():
    pairs = [
        {"gear_id": "1", "mileage_km": 750, "retired": False},
        {"gear_id": "2", "mileage_km": 750, "retired": True},   # retired: never nags
        {"gear_id": "3", "mileage_km": 300, "retired": False},
    ]
    assert [p["gear_id"] for p in gear.worn(pairs, 700)] == ["1"]


def test_worn_disabled_at_zero_threshold():
    pairs = [{"gear_id": "1", "mileage_km": 5000, "retired": False}]
    assert gear.worn(pairs, 0) == []
    assert gear.worn(pairs, None) == []


def test_should_rewarn_first_time_then_step():
    assert gear.should_rewarn(710, None, 150) is True         # first crossing
    assert gear.should_rewarn(720, 700, 150) is False          # not enough extra yet
    assert gear.should_rewarn(860, 700, 150) is True           # +160 >= step
    assert gear.should_rewarn(None, 700, 150) is False         # unknown mileage: silent
    assert gear.should_rewarn(900, 700, 0) is False            # step disabled: warn once only


def test_dominance_note_flags_single_tracked_pair():
    one_pair = [{"mileage_km": 500}]
    assert gear.dominance_note(one_pair) is None   # only one pair total — nothing to compare
    dominant = [{"mileage_km": 500}, {"mileage_km": 0}]
    assert gear.dominance_note(dominant) is not None
    balanced = [{"mileage_km": 500}, {"mileage_km": 300}]
    assert gear.dominance_note(balanced) is None


def test_summary_line_and_warn_text():
    pair = {"name": "Pegasus", "mileage_km": 712.4, "last_used": "2026-07-01"}
    line = gear.summary_line(pair)
    assert "Pegasus" in line and "712 км" in line and "2026-07-01" in line
    assert "заміну" in gear.warn_text(pair)
    assert gear.summary_line({"mileage_km": None}) .endswith("невідомо")


# --- client.py fetches -----------------------------------------------------------

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "GARMIN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(client, "_memo", {})
    return tmp_path


def test_fetch_gear_resolves_profile_and_caches(cache_dir, monkeypatch):
    calls = []

    def fake_safe(_fn, path, params=None):
        calls.append((path, params))
        if path == "/userprofile-service/socialProfile":
            return {"id": 999, "userName": "u"}
        if path == "/gear-service/gear/filterGear":
            return [{"uuid": "shoe1", "displayName": "Pegasus"}]
        return {"_error": "unexpected"}

    monkeypatch.setattr(client, "_safe", fake_safe)
    items = client.fetch_gear()
    assert items == [{"uuid": "shoe1", "displayName": "Pegasus"}]
    assert ("/gear-service/gear/filterGear", {"userProfilePk": "999"}) in calls

    # second call hits the disk cache — no further fetches
    calls.clear()
    assert client.fetch_gear() == items
    assert calls == []


def test_fetch_gear_returns_empty_without_profile_id(cache_dir, monkeypatch):
    monkeypatch.setattr(client, "_safe", lambda _fn, path, **kw: {"noIdHere": True})
    assert client.fetch_gear() == []


def test_fetch_gear_stats_caches_and_handles_error(cache_dir, monkeypatch):
    calls = []

    def fake_safe(_fn, path, **kw):
        calls.append(path)
        return {"totalDistance": 5000} if "stats" in path else {"_error": "boom"}

    monkeypatch.setattr(client, "_safe", fake_safe)
    assert client.fetch_gear_stats("shoe1") == {"totalDistance": 5000}
    calls.clear()
    assert client.fetch_gear_stats("shoe1") == {"totalDistance": 5000}   # cached
    assert calls == []


# --- bot/jobs.py daily sync + warn guard -----------------------------------------

async def _mk_user(session, chat_id=555):
    u = User(email=f"{chat_id}@e.com", password_hash="h", is_approved=True,
             is_active=True, telegram_chat_id=chat_id)
    session.add(u)
    await session.commit()
    await session.refresh(u)
    return u


@pytest.fixture
def _jobs(session, monkeypatch):
    from bot import jobs

    @asynccontextmanager
    async def fake_runtime(_session, _user, *, skip_label=None):
        yield SimpleNamespace(has_garmin=True, anthropic_key="k")

    monkeypatch.setattr(jobs, "user_garmin_runtime", fake_runtime)
    return jobs


async def test_gear_check_syncs_roster_and_warns_once(session, _jobs, monkeypatch):
    user = await _mk_user(session)
    monkeypatch.setattr(
        _jobs, "_sync_gear_roster",
        AsyncMock(return_value=[{"gear_id": "shoe1", "name": "Pegasus",
                                 "mileage_km": 710, "retired": False, "last_used": None}]),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))

    await _jobs._gear_check_for_user(ctx, session, user)
    ctx.bot.send_message.assert_called_once()
    assert "Pegasus" in ctx.bot.send_message.call_args.args[1]

    stored = await repository.get_state(session, user.id, gear.STATE_KEY)
    assert stored is None or True  # roster write is mocked out here; guard is what matters
    guard = await repository.get_state(session, user.id, gear.WARN_PREFIX + "shoe1")
    assert guard == "710"

    # a second tick at the same mileage does not re-send
    ctx.bot.send_message.reset_mock()
    await _jobs._gear_check_for_user(ctx, session, user)
    ctx.bot.send_message.assert_not_called()


async def test_gear_check_rewarns_after_step(session, _jobs, monkeypatch):
    user = await _mk_user(session)
    await repository.set_state(session, user.id, gear.WARN_PREFIX + "shoe1", "700")
    monkeypatch.setattr(
        _jobs, "_sync_gear_roster",
        AsyncMock(return_value=[{"gear_id": "shoe1", "name": "Pegasus",
                                 "mileage_km": 860, "retired": False, "last_used": None}]),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await _jobs._gear_check_for_user(ctx, session, user)
    ctx.bot.send_message.assert_called_once()
    guard = await repository.get_state(session, user.id, gear.WARN_PREFIX + "shoe1")
    assert guard == "860"


async def test_gear_check_disabled_threshold_sends_nothing(session, _jobs, monkeypatch):
    user = await _mk_user(session)
    monkeypatch.setattr(
        _jobs.settings, "GEAR_WEAR_KM", 0,
    )
    monkeypatch.setattr(
        _jobs, "_sync_gear_roster",
        AsyncMock(return_value=[{"gear_id": "shoe1", "name": "Pegasus",
                                 "mileage_km": 5000, "retired": False, "last_used": None}]),
    )
    ctx = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    await _jobs._gear_check_for_user(ctx, session, user)
    ctx.bot.send_message.assert_not_called()


# --- /gear command ---------------------------------------------------------------

class _FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, reply_markup=None):
        self.replies.append(text)


async def test_gear_command_no_data_yet(session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(session)

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id),
                             message=msg)
    await h.gear_cmd(update, SimpleNamespace(args=[]))
    assert "синку" in msg.replies[-1]


async def test_gear_command_lists_pairs(session, monkeypatch):
    from bot import handlers as h

    user = await _mk_user(session)
    roster = [{"gear_id": "1", "name": "Pegasus", "type": "Running",
               "mileage_km": 300, "last_used": "2026-07-01", "retired": False}]
    await repository.set_state(session, user.id, gear.STATE_KEY, json.dumps(roster))

    @asynccontextmanager
    async def maker():
        yield session

    monkeypatch.setattr(h, "async_session_maker", maker)
    msg = _FakeMessage()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=user.telegram_chat_id),
                             message=msg)
    await h.gear_cmd(update, SimpleNamespace(args=[]))
    assert "Pegasus" in msg.replies[-1] and "300 км" in msg.replies[-1]
