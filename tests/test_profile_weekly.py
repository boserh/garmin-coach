"""EP-18 phase 2 · the weekly coach-memory pass.

Phase 1 stored and injected facts; this is the pass that ACCUMULATES them — one Claude call a
week inside the digest job. Its failure mode is the same as phase 1's and worse, because it is
self-feeding: a wrong conclusion, re-confirmed by its own presence in the prompt, steers advice
for months. So the tests here are mostly guards — evidence required, the stop-list holds, a
malformed week changes nothing, the cap is enforced in code rather than requested in the
prompt, and a failure cannot cost the digest.

The Claude call is mocked throughout: the suite spends $0.
"""
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet

from app.analysis import reports
from app.analysis.client import CallStats
from app.core import crypto
from app.db import profile as profile_db
from app.db.models import ReportLog, User


@pytest.fixture
def secret_key(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    yield
    monkeypatch.setattr(crypto, "_fernet", None)


async def _user(session, email="weekly@example.com") -> User:
    u = User(email=email, password_hash="x", is_active=True)
    session.add(u)
    await session.flush()
    return u


async def _report(session, user_id, text="Сьогодні відновлення слабке.", kind="report"):
    row = ReportLog(user_id=user_id, kind=kind, model="claude-sonnet-5", ok=True,
                    report_text=text, created_at=dt.datetime.now(dt.timezone.utc))
    session.add(row)
    await session.flush()
    return row


def _delta_reply(payload: str):
    def _fake(context, api_key=None):
        _fake.context = context
        return payload, CallStats(kind="profile", model="claude-sonnet-5")
    return _fake


# ---------- parsing ----------

def test_parses_a_delta_out_of_fenced_json():
    delta = reports.parse_profile_delta(
        '```json\n{"add": [{"text": "t", "kind": "response", "evidence": [1]}], '
        '"confirm": ["abc"], "contradict": [], "drop": []}\n```')
    assert delta["add"][0]["text"] == "t"
    assert delta["confirm"] == ["abc"]


def test_unparseable_reply_changes_nothing():
    """A malformed weekly pass must leave the profile exactly as it was — never half
    applied."""
    assert reports.parse_profile_delta("вибач, не можу") == {}
    assert reports.parse_profile_delta("") == {}
    assert reports.parse_profile_delta("{not json}") == {}


def test_the_three_fact_cap_is_enforced_in_code():
    """The prompt asks for at most three; the code guarantees it. A profile that can grow by
    an arbitrary number of facts per week defeats both ceilings it lives under."""
    many = ", ".join(
        f'{{"text": "f{i}", "kind": "context", "evidence": [1]}}' for i in range(10))
    delta = reports.parse_profile_delta(f'{{"add": [{many}]}}')
    assert len(delta["add"]) == reports.PROFILE_MAX_ADDS


def test_garbage_list_entries_are_dropped():
    delta = reports.parse_profile_delta(
        '{"add": ["not a dict"], "confirm": [1, "ok"], "drop": null}')
    assert delta["add"] == []
    assert delta["confirm"] == ["ok"]
    assert delta["drop"] == []


# ---------- the pass itself ----------

@pytest.mark.asyncio
async def test_a_quiet_week_costs_nothing(session, secret_key):
    """No reports in the window → no call at all. The cheapest pass is the one that doesn't
    happen."""
    user = await _user(session, "quiet@example.com")
    called = AsyncMock()
    with patch.object(reports, "profile_update_with_stats", called):
        result = await reports.run_profile_update(session, user_id=user.id, api_key="k")
    assert result is None
    called.assert_not_called()


@pytest.mark.asyncio
async def test_one_call_a_week_stores_the_facts_it_proposes(session, secret_key):
    user = await _user(session, "learns@example.com")
    row = await _report(session, user.id, "Понеділкові сесії знову пропущені.")

    fake = _delta_reply(
        '{"add": [{"text": "стабільно пропускає сесії в понеділок", '
        f'"kind": "preference", "confidence": 0.5, "evidence": [{row.id}]}}], '
        '"confirm": [], "contradict": [], "drop": []}')
    with patch.object(reports, "profile_update_with_stats", fake):
        delta = await reports.run_profile_update(session, user_id=user.id, api_key="k")

    assert len(delta["add"]) == 1
    facts, _stop = await profile_db.get_profile(session, user.id)
    assert [f["text"] for f in facts] == ["стабільно пропускає сесії в понеділок"]
    # The evidence is a real report_logs id, so the claim leads back to text a human can read.
    assert facts[0]["evidence"] == [row.id]
    # ...and the week's reports were what the model was given to cite.
    assert row.id in {r["id"] for r in fake.context["reports"]}


@pytest.mark.asyncio
async def test_a_fact_without_evidence_is_never_stored(session, secret_key):
    """The central anti-poisoning rule, end to end: the model proposing a claim is not enough,
    it has to point at the report it came from."""
    user = await _user(session, "noevidence@example.com")
    await _report(session, user.id)

    fake = _delta_reply(
        '{"add": [{"text": "любить довгі в неділю", "kind": "preference", '
        '"confidence": 0.9, "evidence": []}]}')
    with patch.object(reports, "profile_update_with_stats", fake):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    facts, _stop = await profile_db.get_profile(session, user.id)
    assert facts == []


@pytest.mark.asyncio
async def test_a_rejected_fact_cannot_come_back_next_week(session, secret_key):
    """The AC that "це неправда" is permanent: the same statement re-proposed weeks later is
    refused by the stop-list, not stored again."""
    from app import profile as profile_rules

    user = await _user(session, "stoplist@example.com")
    row = await _report(session, user.id)
    text = "коліно ниє на спусках"
    await profile_db.save_profile(session, user.id, [], [profile_rules.fact_id(text)])

    fake = _delta_reply(
        f'{{"add": [{{"text": "{text}", "kind": "constraint", "confidence": 0.8, '
        f'"evidence": [{row.id}]}}]}}')
    with patch.object(reports, "profile_update_with_stats", fake):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    facts, stop = await profile_db.get_profile(session, user.id)
    assert facts == []
    assert profile_rules.fact_id(text) in stop


@pytest.mark.asyncio
async def test_contradiction_lowers_confidence_rather_than_erasing(session, secret_key):
    """One contradicting week is evidence, not a verdict — a claim that keeps being
    contradicted decays out of the top-25 on its own instead of vanishing at the first
    disagreement."""
    from app import profile as profile_rules

    user = await _user(session, "contradict@example.com")
    row = await _report(session, user.id)
    fact = profile_rules.normalize_fact({
        "text": "після темпових середа завжди провальна", "kind": "response",
        "confidence": 0.8, "evidence": [row.id]})
    await profile_db.save_profile(session, user.id, [fact], [])

    fake = _delta_reply(f'{{"contradict": ["{fact["id"]}"]}}')
    with patch.object(reports, "profile_update_with_stats", fake):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    facts, _stop = await profile_db.get_profile(session, user.id)
    assert len(facts) == 1
    assert facts[0]["confidence"] < 0.8


@pytest.mark.asyncio
async def test_the_pass_is_logged_for_cost_and_audit(session, secret_key):
    """Every Claude call in the app leaves a report_logs row — that's the audit trail the
    cost rules rest on."""
    from sqlalchemy import select

    user = await _user(session, "logged@example.com")
    await _report(session, user.id)
    with patch.object(reports, "profile_update_with_stats", _delta_reply('{"add": []}')):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    kinds = (await session.execute(
        select(ReportLog.kind).where(ReportLog.user_id == user.id))).scalars().all()
    assert "profile" in kinds


@pytest.mark.asyncio
async def test_a_failed_update_never_costs_the_digest(session):
    """An AC: the weekly pass rides on the digest job, and a failure there leaves the profile
    at yesterday's state instead of breaking the message the user actually waits for."""
    from bot import jobs as jobs_mod

    user = await _user(session, "resilient@example.com")
    creds = SimpleNamespace(anthropic_key="k")
    with patch.object(jobs_mod, "run_profile_update",
                      AsyncMock(side_effect=RuntimeError("boom"))):
        await jobs_mod._profile_update_for_user(session, user, creds)   # must not raise


@pytest.mark.asyncio
async def test_the_pass_sees_what_is_already_known(session, secret_key):
    """A delta needs the current facts in front of it — otherwise the model re-proposes what
    it already said last week and the profile fills up with paraphrases."""
    from app import profile as profile_rules

    user = await _user(session, "context@example.com")
    row = await _report(session, user.id)
    fact = profile_rules.normalize_fact({
        "text": "інтервали в спеку стабільно зриває", "kind": "response",
        "confidence": 0.7, "evidence": [row.id]})
    await profile_db.save_profile(session, user.id, [fact], [])

    fake = _delta_reply('{"add": []}')
    with patch.object(reports, "profile_update_with_stats", fake):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    known = {f["text"] for f in fake.context["profile"]}
    assert "інтервали в спеку стабільно зриває" in known


@pytest.mark.asyncio
async def test_the_pass_commits_what_it_learned(session, secret_key):
    """The regression behind the Sunday-evening ops alert ("DIGEST user=N left uncommitted
    writes: AthleteProfile — discarded on close"): the pass ran, paid for a Sonnet call and
    then handed the new profile to a session nobody committed. It rides at the very tail of
    the digest job, so there is no later write to flush it — the memory was thrown away every
    week. A rollback here stands in for the job closing its session."""
    user = await _user(session, "commits@example.com")
    row = await _report(session, user.id, "Знову зрив довгої після нічної зміни.")

    fake = _delta_reply(
        '{"add": [{"text": "нічні зміни зривають довгу", "kind": "context", '
        f'"confidence": 0.6, "evidence": [{row.id}]}}]}}')
    with patch.object(reports, "profile_update_with_stats", fake):
        await reports.run_profile_update(session, user_id=user.id, api_key="k")

    assert not session.dirty and not session.new     # nothing left for close() to discard
    await session.rollback()
    facts, _stop = await profile_db.get_profile(session, user.id)
    assert [f["text"] for f in facts] == ["нічні зміни зривають довгу"]
