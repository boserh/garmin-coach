"""Supplement tracking + on-demand lab-monitoring advice (the "Аналізи" tab's third
follow-up): `app.db.supplements` CRUD, `run_supplement_advice` (payload shaping,
dedup-cache, no-active-supplements early-out), and the `/checkups/supplements` routes."""
from unittest.mock import patch

from app.analysis import reports
from app.analysis.client import CallStats
from app.db import checkups as checkups_db
from app.db import supplements as supplements_db
from app.db.models import HealthCheckup, Supplement
from tests.web_helpers import _user_id

U1 = 1


# --- app.db.supplements CRUD ------------------------------------------------------

async def _supplement(session, **kw):
    row = Supplement(user_id=U1, name=kw.pop("name", "Вітамін D3"), **kw)
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def test_create_and_get_supplement(session):
    row = await supplements_db.create_supplement(
        session, U1, name="Магній", dosage="400 мг", frequency="щодня")
    fetched = await supplements_db.get_supplement(session, U1, row.id)
    assert fetched.name == "Магній" and fetched.dosage == "400 мг"


async def test_get_supplement_scoped_by_user(session):
    row = await _supplement(session)
    assert await supplements_db.get_supplement(session, 999, row.id) is None


async def test_list_supplements_active_first_then_stopped(session):
    active = await _supplement(session, name="Активна")
    stopped = await _supplement(session, name="Припинена", is_active=False)
    rows = await supplements_db.list_supplements(session, U1)
    assert rows[0].id == active.id and rows[-1].id == stopped.id

    active_only = await supplements_db.list_supplements(session, U1, active_only=True)
    assert stopped.id not in [r.id for r in active_only]


async def test_update_supplement(session):
    row = await _supplement(session, name="Стара назва")
    await supplements_db.update_supplement(
        session, row, name="Нова назва", dosage="1000 мг", is_active=False)
    assert row.name == "Нова назва" and row.dosage == "1000 мг" and row.is_active is False


async def test_delete_supplement(session):
    row = await _supplement(session)
    await supplements_db.delete_supplement(session, row)
    assert await supplements_db.get_supplement(session, U1, row.id) is None


# --- app.db.checkups.recent_categories ---------------------------------------------

async def test_recent_categories_dedupes_and_falls_back_to_title(session):
    import datetime as dt

    today = dt.date.today().isoformat()
    session.add_all([
        HealthCheckup(user_id=U1, date=today, title="Кров 1", category="кров"),
        HealthCheckup(user_id=U1, date=today, title="Кров 2", category="кров"),
        HealthCheckup(user_id=U1, date=today, title="Огляд щитоподібної"),  # no category
    ])
    await session.commit()
    cats = await checkups_db.recent_categories(session, U1)
    assert set(cats) == {"кров", "Огляд щитоподібної"}  # order among same-date ties is unspecified
    assert len(cats) == 2  # the two "кров" rows dedupe to one entry


async def test_recent_categories_excludes_old_entries(session):
    import datetime as dt

    old = (dt.date.today() - dt.timedelta(days=1000)).isoformat()
    session.add(HealthCheckup(user_id=U1, date=old, title="Старий", category="давнє"))
    await session.commit()
    assert await checkups_db.recent_categories(session, U1) == []


# --- run_supplement_advice ---------------------------------------------------------

def test_supplement_payload_shapes_active_list():
    supps = [Supplement(id=1, name="D3", dosage="5000 МО", frequency="щодня")]
    data = reports.supplement_payload(supps, ["кров"])
    assert data["supplements"] == [{"name": "D3", "dosage": "5000 МО", "frequency": "щодня"}]
    assert data["recent_checkup_categories"] == ["кров"]


def test_supplement_payload_omits_empty_history():
    data = reports.supplement_payload([Supplement(id=1, name="D3")])
    assert "recent_checkup_categories" not in data


async def test_run_supplement_advice_returns_none_without_active_supplements(session):
    await _supplement(session, is_active=False)  # only a stopped one
    result = await reports.run_supplement_advice(session, user_id=U1, api_key="k")
    assert result is None


async def test_run_supplement_advice_caches_and_logs(session, monkeypatch):
    await _supplement(session, name="D3", dosage="5000 МО")
    calls = {"n": 0}

    def fake_with_stats(context, api_key=None):
        calls["n"] += 1
        return f"порада #{calls['n']}", CallStats(kind="supplements", model="m")

    monkeypatch.setattr(reports, "supplement_advice_with_stats", fake_with_stats)

    text1 = await reports.run_supplement_advice(session, user_id=U1, api_key="k")
    assert text1 == "порада #1" and calls["n"] == 1

    # unchanged active list -> dedup-cache hit, no new Claude call
    text2 = await reports.run_supplement_advice(session, user_id=U1, api_key="k")
    assert text2 == "порада #1" and calls["n"] == 1

    from sqlalchemy import select

    from app.db.models import ReportLog

    logged = (await session.execute(
        select(ReportLog).where(ReportLog.kind == "supplements"))).scalars().all()
    assert len(logged) == 2  # one real call + one cache hit, both logged


# --- /checkups/supplements routes ---------------------------------------------------

def _get_supp_id_by_name(uid, name):
    import anyio

    from app.db.base import async_session_maker

    async def get_id():
        async with async_session_maker() as s:
            rows = await supplements_db.list_supplements(s, uid)
            return next(r.id for r in rows if r.name == name)

    return anyio.run(get_id)


def test_supplements_requires_login(client):
    assert client.get("/checkups/supplements", follow_redirects=False).status_code == 303


def test_supplements_empty_state(auth_client):
    assert "Ще немає жодної добавки" in auth_client.get("/checkups/supplements").text


def test_create_list_edit_delete_supplement(auth_client):
    r = auth_client.post(
        "/checkups/supplements",
        data={"name": "Омега-3", "dosage": "1000 мг", "frequency": "щодня"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/checkups/supplements?saved=1"

    listing = auth_client.get("/checkups/supplements").text
    assert "Омега-3" in listing and "1000 мг" in listing

    uid = _user_id("t@example.com")
    sid = _get_supp_id_by_name(uid, "Омега-3")

    auth_client.post(f"/checkups/supplements/{sid}",
                     data={"name": "Омега-3 (нова доза)", "dosage": "2000 мг"})
    assert "Омега-3 (нова доза)" in auth_client.get("/checkups/supplements").text

    auth_client.post(f"/checkups/supplements/{sid}/delete")
    assert "Омега-3 (нова доза)" not in auth_client.get("/checkups/supplements").text


def test_missing_name_redirects_with_error(auth_client):
    r = auth_client.post("/checkups/supplements", data={"name": ""}, follow_redirects=False)
    assert r.status_code == 303 and "err=required" in r.headers["location"]


def test_analyze_route_no_active_supplements(auth_client):
    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()):
        r = auth_client.post("/checkups/supplements/analyze", follow_redirects=False)
    assert r.status_code == 303 and "err=none" in r.headers["location"]


def test_analyze_route_stores_and_shows_advice(auth_client):
    auth_client.post("/checkups/supplements", data={"name": "Залізо", "dosage": "30 мг"})

    def fake_with_stats(context, api_key=None):
        return "🔬 контролюй феритин раз на 6 міс", CallStats(kind="supplements", model="m")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()), \
         patch.object(reports, "supplement_advice_with_stats", fake_with_stats):
        r = auth_client.post("/checkups/supplements/analyze", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/checkups/supplements?analyzed=1"

    detail = auth_client.get("/checkups/supplements").text
    assert "контролюй феритин" in detail


def test_supplements_are_isolated_per_user(client):
    from tests.web_helpers import _seed_user

    _seed_user(email="alice2@example.com", password="pw", is_admin=False)
    _seed_user(email="bob2@example.com", password="pw", is_admin=False)

    client.post("/login", data={"email": "alice2@example.com", "password": "pw"})
    client.post("/checkups/supplements", data={"name": "Alice's supplement"})
    client.post("/logout")

    client.post("/login", data={"email": "bob2@example.com", "password": "pw"})
    assert "Alice's supplement" not in client.get("/checkups/supplements").text
