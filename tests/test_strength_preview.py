"""ST-05: strength preview in the setup form — generation wrapper, confirmed-preview
reuse (skip regeneration), and description-hash invalidation (never trust the client)."""
import json
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.analysis import plans
from app.analysis.service import CallStats, run_plan_generation, run_strength_preview
from app.db.models import ReportLog
from app.garmin import repository
from app.garmin.schemas import GeneratedPlan, PlanWorkout, StrengthSession
from app.routers.plan import _confirmed_previews, _desc_hash

U1 = 1


def _sess(name="Ноги"):
    return StrengthSession(name=name, warmup_s=300, blocks=[
        {"reps": 3, "rest_s": 90, "exercises": [
            {"category": "squat", "exercise": "goblet_squat", "reps": 12, "weight_kg": 20}]}])


def _gen():
    return GeneratedPlan(summary="x", workouts=[
        PlanWorkout(date="2026-07-01", week=1, type="easy", dist_km=4.0, description="легко")])


async def test_run_strength_preview_returns_sanitised_and_logs(session):
    with patch.object(plans, "generate_strength_with_stats",
                      return_value=(_sess(), CallStats(kind="plan", model="m"))):
        sp = await run_strength_preview(
            session, user_id=U1, description="силова на ноги", api_key=None)
    assert sp and sp["name"] == "Ноги"
    ex = sp["blocks"][0]["exercises"][0]
    assert ex["category"] == "SQUAT" and ex["exercise"] == "GOBLET_SQUAT"
    logs = (await session.execute(
        select(ReportLog).where(ReportLog.kind == "strength"))).scalars().all()
    assert len(logs) == 1 and logs[0].ok is True


async def test_generation_reuses_confirmed_preview(session):
    """A confirmed preview → generation must NOT call Claude for that session again."""
    confirmed = {"name": "Готова", "warmup_s": 0, "blocks": [
        {"reps": 3, "rest_s": 60, "exercises": [
            {"category": "SQUAT", "exercise": None, "reps": 10, "weight_kg": None}]}]}
    intake = {"strength": {"enabled": True, "custom": {"tue": "ноги"},
                           "custom_generated": {"tue": confirmed}}}
    strength_mock = MagicMock()
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))), \
            patch.object(plans, "generate_strength_with_stats", strength_mock):
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="Перші 5 км",
            target_date="2026-07-20", start_date="2026-06-30", days_per_week=3,
            intensity="moderate", intake=intake, api_key=None,
            run_days=["tue", "thu", "sun"], long_run_day="sun")
    strength_mock.assert_not_called()
    strengths = [w for w in await repository.list_workouts(session, plan.id)
                 if w.type == "strength"]
    assert strengths and strengths[0].strength_plan["name"] == "Готова"


async def test_generation_regenerates_without_preview(session):
    """No confirmed preview → generation falls back to the Claude call."""
    intake = {"strength": {"enabled": True, "custom": {"tue": "ноги"}}}
    with patch.object(plans, "generate_plan_with_stats",
                      return_value=(_gen(), CallStats(kind="plan", model="m"))), \
            patch.object(plans, "generate_strength_with_stats",
                         return_value=(_sess(), CallStats(kind="plan", model="m"))) as m:
        plan = await run_plan_generation(
            session, user_id=U1, goal="first_5k", goal_label="Перші 5 км",
            target_date="2026-07-20", start_date="2026-06-30", days_per_week=3,
            intensity="moderate", intake=intake, api_key=None,
            run_days=["tue", "thu", "sun"], long_run_day="sun")
    m.assert_called()
    strengths = [w for w in await repository.list_workouts(session, plan.id)
                 if w.type == "strength"]
    assert strengths and strengths[0].strength_plan["name"] == "Ноги"


class _FakeReq:
    def __init__(self, data):
        self._d = data

    async def form(self):
        return self._d


async def test_confirmed_previews_hash_match_invalidation_and_untrust():
    desc = "силова на ноги"
    good = json.dumps({"name": "X", "blocks": [
        {"reps": 3, "exercises": [{"category": "SQUAT", "reps": 10}]}]})

    # matching hash → included, and re-sanitised server-side
    req = _FakeReq({"strength_preview_tue": good, "strength_prehash_tue": _desc_hash(desc)})
    out = await _confirmed_previews(req, {"tue": desc})
    assert out["tue"]["blocks"][0]["exercises"][0]["category"] == "SQUAT"

    # description edited after preview → hash mismatch → dropped (regenerate later)
    req2 = _FakeReq({"strength_preview_tue": good, "strength_prehash_tue": _desc_hash(desc)})
    assert await _confirmed_previews(req2, {"tue": "зовсім інша силова"}) == {}

    # client-tampered session (bogus category) survives the hash but not sanitising → dropped
    bad = json.dumps({"blocks": [{"reps": 3, "exercises": [{"category": "BOGUS"}]}]})
    req3 = _FakeReq({"strength_preview_tue": bad, "strength_prehash_tue": _desc_hash(desc)})
    assert await _confirmed_previews(req3, {"tue": desc}) == {}
