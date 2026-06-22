"""build_payload shape/values against a mocked Garmin provider (no network)."""
import datetime as dt

from app.garmin import client, service
from app.garmin.schemas import Payload


class FakeProvider:
    username = "tester"

    def login(self):
        pass

    def connectapi(self, path, **kwargs):
        if "dailySleepData" in path:
            return {"dailySleepDTO": {
                "sleepScores": {"overall": {"value": 82}},
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 7200,
                "remSleepSeconds": 5400,
                "awakeSleepSeconds": 600,
            }}
        if path.startswith("/hrv-service"):
            return {"hrvSummary": {"lastNightAvg": 60, "status": "BALANCED"}}
        if "dailyStress" in path:
            return {"avgStressLevel": 25, "maxStressLevel": 70}
        if "bodyBattery" in path:
            return [{"charged": 50, "drained": 40}]
        if "activities/search/activities" in path:
            return [{
                "startTimeLocal": "2026-06-21 07:00:00",
                "activityType": {"typeKey": "running"},
                "duration": 1800, "distance": 5000,
                "averageHR": 150, "maxHR": 165,
                "activityTrainingLoad": 80.0, "activityId": 111,
            }]
        if "/calendar-service/" in path:
            return {"calendarItems": []}
        return {}


def test_build_payload_shape(monkeypatch):
    fp = FakeProvider()
    monkeypatch.setattr(client, "get_provider", lambda: fp)
    monkeypatch.setattr(service, "get_provider", lambda: fp)

    payload = service.build_payload(days=1, activity_limit=5)

    assert isinstance(payload, Payload)
    assert payload.window_days == 1
    assert payload.synced_today is True
    assert payload.last_data_date == dt.date.today().isoformat()

    day = payload.daily[0]
    assert day.sleep_score == 82
    assert day.sleep_h == 4.5  # (3600 + 7200 + 5400) / 3600
    assert day.hrv_avg == 60
    assert day.hrv_status == "BALANCED"
    assert day.stress_avg == 25
    assert day.has_data is True

    act = payload.recent_activities[0]
    assert act.type == "running"
    assert act.dist_km == 5.0
    assert act.avg_hr == 150
    # non-strength activities carry no `exercises` key (matches original shape)
    assert "exercises" not in act.model_dump()

    assert payload.planned_runs == []


def test_fetch_workout_detail_parses_description(monkeypatch):
    raw = {
        "workoutName": "W1 Mon Easy Run - 3km Easy Run",
        "description": "Run Further Plan (Week 1/10)\n\n3km easy run at a "
                       "conversational pace (no faster than 7:15/km). A limit, not a target.",
        "workoutSegments": [{"workoutSteps": [
            {"endConditionValue": 3000.0, "targetValueOne": None, "targetValueTwo": None},
        ]}],
    }

    class P:
        username = "t"

        def login(self):
            pass

        def connectapi(self, path, **kwargs):
            return raw

    monkeypatch.setattr(client, "get_provider", lambda: P())
    monkeypatch.setattr(client, "_cache_get", lambda k: None)   # no disk
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)

    d = client.fetch_workout_detail(123)
    assert d["name"] == "W1 Mon Easy Run - 3km Easy Run"
    assert "no faster than 7:15/km" in d["description"]
    assert d["steps"][0]["dist_m"] == 3000.0
    assert d["steps"][0]["pace_min_km"] is None


def test_payload_dump_keys_are_stable(monkeypatch):
    fp = FakeProvider()
    monkeypatch.setattr(client, "get_provider", lambda: fp)
    monkeypatch.setattr(service, "get_provider", lambda: fp)

    payload = service.build_payload(days=1, activity_limit=5)
    dumped = payload.model_dump()
    assert set(dumped) == {
        "generated", "window_days", "synced_today", "last_data_date",
        "daily", "recent_activities", "planned_runs",
    }
