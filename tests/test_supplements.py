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


async def test_run_supplement_advice_force_bypasses_cache(session, monkeypatch):
    """A fixed max_tokens once truncated a long list mid-sentence and the bad text sat in
    the dedup cache — force=True is the escape hatch to actually regenerate."""
    await _supplement(session, name="D3", dosage="5000 МО")
    calls = {"n": 0}

    def fake_with_stats(context, api_key=None):
        calls["n"] += 1
        return f"порада #{calls['n']}", CallStats(kind="supplements", model="m")

    monkeypatch.setattr(reports, "supplement_advice_with_stats", fake_with_stats)

    text1 = await reports.run_supplement_advice(session, user_id=U1, api_key="k")
    assert text1 == "порада #1" and calls["n"] == 1

    text2 = await reports.run_supplement_advice(
        session, user_id=U1, api_key="k", force=True)
    assert text2 == "порада #2" and calls["n"] == 2

    # a following non-force call is a cache hit of the FRESH text
    text3 = await reports.run_supplement_advice(session, user_id=U1, api_key="k")
    assert text3 == "порада #2" and calls["n"] == 2


def test_supplement_advice_max_tokens_scales_with_supplement_count():
    """A flat max_tokens=700 silently truncated advice for a long supplement list
    (stop_reason=max_tokens) — it must grow with the item count."""
    valid_json = '{"items": [], "closing_note": "орієнтир"}'
    with patch.object(reports, "_complete") as mocked:
        mocked.return_value = (valid_json, CallStats(kind="supplements", model="m"))
        reports.supplement_advice_with_stats({"supplements": [{"name": "D3"}]})
        small_tokens = mocked.call_args.kwargs["max_tokens"]

        mocked.reset_mock()
        mocked.return_value = (valid_json, CallStats(kind="supplements", model="m"))
        many = [{"name": f"S{i}"} for i in range(12)]
        reports.supplement_advice_with_stats({"supplements": many})
        large_tokens = mocked.call_args.kwargs["max_tokens"]

    assert large_tokens > small_tokens
    assert large_tokens <= 2200  # still capped


# --- structured SupplementAdvice: coercion, retry, parse, template building -------

def test_supplement_advice_with_stats_parses_valid_json():
    valid_json = (
        '{"items": [{"supplement": "D3", "marker": "25-OH вітамін D", '
        '"frequency": "раз на рік", "note": null}], "closing_note": "орієнтир"}'
    )
    with patch.object(reports, "_complete") as mocked:
        mocked.return_value = (valid_json, CallStats(kind="supplements", model="m"))
        text, _ = reports.supplement_advice_with_stats({"supplements": [{"name": "D3"}]})
    advice = reports.parse_supplement_advice(text)
    assert advice.items[0].marker == "25-OH вітамін D"
    assert advice.closing_note == "орієнтир"
    assert mocked.call_count == 1  # valid on the first try — no retry needed


def test_supplement_advice_with_stats_retries_once_on_bad_json():
    valid_json = '{"items": [], "closing_note": "орієнтир"}'
    with patch.object(reports, "_complete") as mocked:
        mocked.side_effect = [
            ("не json взагалі", CallStats(kind="supplements", model="m")),
            (valid_json, CallStats(kind="supplements", model="m")),
        ]
        text, _ = reports.supplement_advice_with_stats({"supplements": [{"name": "D3"}]})
    assert mocked.call_count == 2
    assert reports.parse_supplement_advice(text).closing_note == "орієнтир"


def test_supplement_advice_with_stats_raises_after_two_bad_replies():
    from app.analysis.client import AnalystError

    with patch.object(reports, "_complete") as mocked:
        mocked.return_value = ("не json", CallStats(kind="supplements", model="m"))
        try:
            reports.supplement_advice_with_stats({"supplements": [{"name": "D3"}]})
            raise AssertionError("expected AnalystError")
        except AnalystError:
            pass
    assert mocked.call_count == 2


def test_parse_supplement_advice_none_on_legacy_prose():
    """A ReportLog written before this JSON format shipped (plain prose) must not crash
    the page — just stop offering structured items/a template."""
    assert reports.parse_supplement_advice("💊 стара порада текстом, без JSON") is None


def test_supplement_advice_to_checkup_template_dedupes_and_skips_none_markers():
    from app.garmin.schemas import SupplementAdvice, SupplementAdviceItem

    advice = SupplementAdvice(items=[
        SupplementAdviceItem(supplement="D3", marker="25-OH вітамін D",
                             frequency="раз на рік"),
        SupplementAdviceItem(supplement="Кальцій", marker="25-OH вітамін D",
                             frequency="раз на рік"),  # duplicate marker -> deduped
        SupplementAdviceItem(supplement="Мультивітамін", marker=None,
                             note="без специфічного показника"),
    ])
    tmpl = reports.supplement_advice_to_checkup_template(advice)
    assert tmpl["title"] == "Рекомендовані аналізи (за добавками)"
    assert [r["name"] for r in tmpl["results"]] == ["25-OH вітамін D"]
    assert all(r["value"] == "" for r in tmpl["results"])
    assert "D3" in tmpl["notes"]


def test_supplement_advice_to_checkup_template_none_when_no_markers():
    from app.garmin.schemas import SupplementAdvice, SupplementAdviceItem

    advice = SupplementAdvice(items=[
        SupplementAdviceItem(supplement="Мультивітамін", marker=None),
    ])
    assert reports.supplement_advice_to_checkup_template(advice) is None


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
    assert "Спробувати ще раз" in detail  # button relabels once advice exists


def test_analyze_route_force_regenerates(auth_client):
    auth_client.post("/checkups/supplements", data={"name": "Залізо", "dosage": "30 мг"})
    responses = iter(["перша спроба", "друга спроба"])

    def fake_with_stats(context, api_key=None):
        return next(responses), CallStats(kind="supplements", model="m")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()), \
         patch.object(reports, "supplement_advice_with_stats", fake_with_stats):
        auth_client.post("/checkups/supplements/analyze")
        assert "перша спроба" in auth_client.get("/checkups/supplements").text

        # without force, an unchanged list would replay the cached first attempt;
        # force=1 (the "Спробувати ще раз" button) bypasses that and gets fresh text
        auth_client.post("/checkups/supplements/analyze", data={"force": "1"})
    assert "друга спроба" in auth_client.get("/checkups/supplements").text


def test_analyze_route_shows_structured_items_and_template_button(auth_client):
    auth_client.post("/checkups/supplements", data={"name": "Залізо", "dosage": "30 мг"})
    valid_json = (
        '{"items": [{"supplement": "Залізо", "marker": "Феритин", '
        '"frequency": "раз на 6 міс", "note": null}], "closing_note": "не медичне призначення"}'
    )

    def fake_with_stats(context, api_key=None):
        return valid_json, CallStats(kind="supplements", model="m")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()), \
         patch.object(reports, "supplement_advice_with_stats", fake_with_stats):
        auth_client.post("/checkups/supplements/analyze")

    detail = auth_client.get("/checkups/supplements").text
    assert "Феритин" in detail and "раз на 6 міс" in detail
    assert "не медичне призначення" in detail
    assert "Створити шаблон аналізу" in detail


def test_apply_template_route_creates_checkup_and_redirects(auth_client):
    auth_client.post("/checkups/supplements", data={"name": "Залізо", "dosage": "30 мг"})
    valid_json = (
        '{"items": [{"supplement": "Залізо", "marker": "Феритин", '
        '"frequency": "раз на 6 міс", "note": null}], "closing_note": "орієнтир"}'
    )

    def fake_with_stats(context, api_key=None):
        return valid_json, CallStats(kind="supplements", model="m")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()), \
         patch.object(reports, "supplement_advice_with_stats", fake_with_stats):
        auth_client.post("/checkups/supplements/analyze")

    r = auth_client.post("/checkups/supplements/apply-template", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/checkups/")

    detail = auth_client.get(r.headers["location"]).text
    assert "Рекомендовані аналізи" in detail
    assert "Феритин" in detail


def test_apply_template_route_without_advice_redirects_with_error(client):
    # a fresh user (never called /analyze) — earlier tests share t@example.com's DB
    # state within this module, so a prior advice for that user would false-pass this.
    from tests.web_helpers import _seed_user

    _seed_user(email="no-advice-yet@example.com", password="pw", is_admin=False)
    client.post("/login", data={"email": "no-advice-yet@example.com", "password": "pw"})
    r = client.post("/checkups/supplements/apply-template", follow_redirects=False)
    assert r.status_code == 303 and "err=notemplate" in r.headers["location"]


def test_apply_template_route_all_markers_none_redirects_with_error(auth_client):
    auth_client.post("/checkups/supplements", data={"name": "Мультивітамін"})
    valid_json = (
        '{"items": [{"supplement": "Мультивітамін", "marker": null, '
        '"note": "без специфічного показника"}], "closing_note": "орієнтир"}'
    )

    def fake_with_stats(context, api_key=None):
        return valid_json, CallStats(kind="supplements", model="m")

    with patch("app.routers.checkups.load_credentials",
              return_value=type("C", (), {"anthropic_key": "test-key"})()), \
         patch.object(reports, "supplement_advice_with_stats", fake_with_stats):
        auth_client.post("/checkups/supplements/analyze")

    r = auth_client.post("/checkups/supplements/apply-template", follow_redirects=False)
    assert r.status_code == 303 and "err=notemplate" in r.headers["location"]


def test_supplements_are_isolated_per_user(client):
    from tests.web_helpers import _seed_user

    _seed_user(email="alice2@example.com", password="pw", is_admin=False)
    _seed_user(email="bob2@example.com", password="pw", is_admin=False)

    client.post("/login", data={"email": "alice2@example.com", "password": "pw"})
    client.post("/checkups/supplements", data={"name": "Alice's supplement"})
    client.post("/logout")

    client.post("/login", data={"email": "bob2@example.com", "password": "pw"})
    assert "Alice's supplement" not in client.get("/checkups/supplements").text
