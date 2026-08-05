"""EP-18 phase 1 · coach memory — storage, eviction and injection.

The feature's failure mode is **poisoning**: one wrong conclusion, kept alive by its own
presence in the prompt, steering advice for months. Most of these tests are the guards
against that (evidence required, decay, contradiction lowers rather than rewrites, a
rejected fact cannot come back) plus the two hard ceilings, which are tested rather than
agreed because an unbounded profile inflates the input tokens of every call in the app.
"""
import datetime as dt

import pytest
from cryptography.fernet import Fernet

from app import profile
from app.core import crypto
from app.db import profile as profile_db
from app.db.models import User

TODAY = dt.date(2026, 8, 5)


@pytest.fixture
def secret_key(monkeypatch):
    """Facts are encrypted at rest; give the module a real key (the suite deliberately runs
    without one by default)."""
    monkeypatch.setattr(crypto, "_fernet", None)
    monkeypatch.setattr(crypto.settings, "APP_SECRET_KEY", Fernet.generate_key().decode())
    yield
    monkeypatch.setattr(crypto, "_fernet", None)


def _fact(text="після темпових середа завжди провальна", kind="response",
          confidence=0.7, last_confirmed=None, **kw):
    return profile.normalize_fact({
        "text": text, "kind": kind, "confidence": confidence, "evidence": [1],
        "first_seen": "2026-01-01",
        "last_confirmed": (last_confirmed or TODAY.isoformat()),
        **kw,
    }, today=TODAY)


async def _user(session, email="p@example.com") -> User:
    u = User(email=email, password_hash="x")
    session.add(u)
    await session.commit()
    return u


# ---------- validation: the anti-poisoning rules ----------

def test_fact_without_evidence_is_rejected():
    """The main defence: every remembered claim must trace back to a report_logs row a
    human can go and read. No evidence, no memory."""
    assert profile.normalize_fact(
        {"text": "любить довгі", "kind": "preference", "confidence": 0.9}, today=TODAY
    ) is None


def test_fact_with_an_unknown_kind_is_rejected():
    assert profile.normalize_fact(
        {"text": "щось", "kind": "vibes", "evidence": [1]}, today=TODAY) is None


def test_overlong_and_empty_facts_are_rejected():
    assert profile.normalize_fact(
        {"text": "", "kind": "context", "evidence": [1]}, today=TODAY) is None
    assert profile.normalize_fact(
        {"text": "x" * 500, "kind": "context", "evidence": [1]}, today=TODAY) is None


def test_same_statement_gets_the_same_id_regardless_of_punctuation():
    """Identity has to survive rewording noise, otherwise the stop-list leaks and the same
    rejected claim reappears with a full stop attached."""
    assert profile.fact_id("Коліно ниє на спусках.") == profile.fact_id("коліно ниє на спусках")


# ---------- ceilings ----------

def test_ceilings_hold_under_a_hundred_facts():
    """AC: 100 generated facts → at most 25 travel and at most 1200 tokens."""
    facts = [_fact(text=f"факт номер {i} про реакцію на навантаження", confidence=0.9)
             for i in range(100)]
    chosen = profile.select(facts, today=TODAY)
    assert len(chosen) <= profile.MAX_FACTS
    total = sum(profile.estimate_tokens(f["text"]) + 4 for f in chosen)
    assert total <= profile.MAX_TOKENS


def test_long_facts_hit_the_token_ceiling_before_the_count_one():
    facts = [_fact(text=("дуже довгий факт про реакцію на навантаження " * 4)[:230] + f" {i}",
                   confidence=0.9)
             for i in range(profile.MAX_FACTS)]
    chosen = profile.select(facts, today=TODAY)
    assert len(chosen) < profile.MAX_FACTS      # cut by tokens, not by count
    assert sum(profile.estimate_tokens(f["text"]) + 4 for f in chosen) <= profile.MAX_TOKENS


def test_empty_profile_produces_no_block_at_all():
    """AC: a new user's prompts must be byte-for-byte what they were — the field is ABSENT,
    not present-and-empty."""
    assert profile.to_context([]) is None


# ---------- decay + eviction ----------

def test_confidence_decays_with_age():
    fresh = _fact(confidence=0.8, last_confirmed=TODAY.isoformat())
    stale = _fact(text="інший факт", confidence=0.8,
                  last_confirmed=(TODAY - dt.timedelta(days=240)).isoformat())
    assert profile.effective_confidence(stale, TODAY) < \
        profile.effective_confidence(fresh, TODAY) / 2


def test_faded_facts_stop_travelling():
    """A claim that simply stopped being re-observed fades out rather than being deleted —
    the honest outcome, and it self-heals if the pattern comes back."""
    old = _fact(confidence=0.3, last_confirmed="2024-01-01")
    assert profile.select([old], today=TODAY) == []


def test_pinned_facts_neither_decay_nor_get_evicted():
    pinned = _fact(text="коліно на спусках", confidence=0.2,
                   last_confirmed="2024-01-01", pinned=True)
    others = [_fact(text=f"свіжий факт {i}", confidence=1.0) for i in range(40)]
    chosen = profile.select([*others, pinned], today=TODAY)
    assert any(f.get("pinned") for f in chosen), "the user overrode the heuristic"


# ---------- delta merge ----------

def test_add_then_readd_is_a_confirmation_not_a_duplicate():
    facts = profile.apply_delta([], {"add": [
        {"text": "після темпових середа провальна", "kind": "response",
         "confidence": 0.5, "evidence": [1]},
    ]}, today=TODAY)
    again = profile.apply_delta(facts, {"add": [
        {"text": "після темпових середа провальна", "kind": "response",
         "confidence": 0.5, "evidence": [2]},
    ]}, today=TODAY)
    assert len(again) == 1
    assert again[0]["confidence"] > facts[0]["confidence"]


def test_contradiction_lowers_confidence_instead_of_rewriting():
    """One contradicting week is evidence, not proof. A claim that keeps being contradicted
    decays out of the top-25 on its own instead of being flipped by a single bad week."""
    facts = [_fact(confidence=0.8)]
    out = profile.apply_delta(facts, {"contradict": [facts[0]["id"]]}, today=TODAY)
    assert 0 < out[0]["confidence"] < 0.8


def test_confirm_refreshes_the_date_and_raises_confidence():
    f = _fact(confidence=0.5, last_confirmed="2026-01-01")
    out = profile.apply_delta([f], {"confirm": [f["id"]]}, today=TODAY)
    assert out[0]["confidence"] > 0.5
    assert out[0]["last_confirmed"] == TODAY.isoformat()


def test_drop_removes_a_fact():
    f = _fact()
    assert profile.apply_delta([f], {"drop": [f["id"]]}, today=TODAY) == []


def test_a_rejected_fact_cannot_be_regenerated():
    """AC: "this isn't true" → the fact disappears from the next call and does NOT come
    back, even if the weekly pass proposes the identical statement weeks later."""
    f = _fact()
    facts, stoplist, removed = profile.forget([f], [], f["id"])
    assert removed and facts == []
    back = profile.apply_delta(facts, {"add": [
        {"text": f["text"], "kind": f["kind"], "confidence": 0.9, "evidence": [7]},
    ]}, today=TODAY, stoplist=stoplist)
    assert back == [], "a rejected statement must not be rediscoverable"


def test_forget_accepts_the_statement_text_too():
    f = _fact()
    _facts, stoplist, removed = profile.forget([f], [], f["text"])
    assert removed and f["id"] in stoplist


def test_delta_never_stores_an_invalid_fact():
    out = profile.apply_delta([], {"add": [
        {"text": "без доказу", "kind": "response", "confidence": 0.9},   # no evidence
        {"text": "поганий вид", "kind": "nonsense", "evidence": [1]},
    ]}, today=TODAY)
    assert out == []


# ---------- storage ----------

@pytest.mark.asyncio
async def test_profile_roundtrip_is_encrypted_at_rest(session, secret_key):
    """This is the most sensitive free text in the DB (injuries, work, habits). An OPS-02
    backup copy is safe to store anywhere precisely because everything sensitive in it is
    Fernet-encrypted — the profile must not be the exception that breaks that."""
    u = await _user(session)
    f = _fact(text="коліно ниє на спусках", kind="constraint")
    await profile_db.save_profile(session, u.id, [f], [])
    await session.commit()

    row = await profile_db.get_row(session, u.id)
    assert "коліно" not in (row.facts_enc or ""), "stored plaintext would defeat the point"
    facts, stoplist = await profile_db.get_profile(session, u.id)
    assert facts[0]["text"] == "коліно ниє на спусках" and stoplist == []


@pytest.mark.asyncio
async def test_missing_profile_reads_as_empty(session):
    assert await profile_db.get_profile(session, 4242) == ([], [])
    assert await profile_db.build_context(session, 4242) is None


@pytest.mark.asyncio
async def test_undecryptable_blob_degrades_to_empty(session, secret_key):
    """A keyless install (or a rotated key) still gets its morning report — with an
    amnesiac coach rather than a traceback."""
    u = await _user(session, email="rot@example.com")
    await profile_db.save_profile(session, u.id, [_fact()], [])
    await session.commit()
    row = await profile_db.get_row(session, u.id)
    row.facts_enc = "not-a-fernet-token"
    await session.commit()
    facts, _ = await profile_db.get_profile(session, u.id)
    assert facts == []


@pytest.mark.asyncio
async def test_profile_is_user_scoped(session, secret_key):
    a = await _user(session, email="a@example.com")
    b = await _user(session, email="b@example.com")
    await profile_db.save_profile(session, a.id, [_fact(text="секрет а")], [])
    await session.commit()
    assert await profile_db.get_profile(session, b.id) == ([], [])
    assert await profile_db.build_context(session, b.id) is None


# ---------- injection ----------

def test_profile_is_in_the_daily_cache_key():
    """AC: change a fact → the same payload must yield a NEW report. Without the profile in
    the key the coach would "learn" something and keep serving the old text."""
    from app.analysis.cache import _cache_key

    base = dict(data={}, question="q", model="m")
    a = _cache_key(**base)
    b = _cache_key(**base, athlete_profile={"facts": [{"id": "x", "text": "t"}]})
    c = _cache_key(**base, athlete_profile={"facts": [{"id": "y", "text": "u"}]})
    assert a != b != c and a != c


def test_profile_is_in_the_ask_cache_key():
    from app.analysis.cache import _ask_cache_key

    base = ([], "q", "m", [])
    assert _ask_cache_key(*base) != _ask_cache_key(
        *base, athlete_profile={"facts": [{"id": "x", "text": "t"}]})


def test_every_advice_prompt_carries_the_memory_block():
    """One block, one wording. Four hand-written copies would drift into four different sets
    of rules about how much memory may override today's data — which is the safety story."""
    from app.analysis import prompts

    for name in ("SYSTEM", "SYSTEM_ASK_TOOLS", "SYSTEM_PLAN", "SYSTEM_PLAN_ADAPT"):
        assert prompts.PROFILE_BLOCK in getattr(prompts, name), name


def test_prompt_puts_fresh_data_above_memory():
    """The ordering rule is what keeps a stale fact from overriding what the watch measured
    this morning."""
    from app.analysis import prompts

    assert "ПРІОРИТЕТ" in prompts.PROFILE_BLOCK
