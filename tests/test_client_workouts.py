"""fetch_workouts pagination — a long-standing routine (e.g. Day 1) must not be dropped
by a single short page (the bug that made the picker show only 'Day 1 manual'/'Day 2')."""
from app.garmin import client


def test_fetch_workouts_paginates_until_short_page(monkeypatch):
    # 250 workouts across 3 pages of 100; Day 1 sits at index 230 (page 3).
    all_w = [{"workoutId": i + 1, "workoutName": f"W{i}",
              "sportType": {"sportTypeKey": "running"}} for i in range(250)]
    all_w[230] = {"workoutId": 931013083, "workoutName": "Day 1",
                  "sportType": {"sportTypeKey": "strength_training"}}

    def fake_safe(_fn, _path, params=None):
        start = (params or {}).get("start", 0)
        page = (params or {}).get("limit", 100)
        return all_w[start:start + page]

    monkeypatch.setattr(client, "_safe", fake_safe)
    out = client.fetch_workouts()
    assert len(out) == 250  # all pages collected, not just the first 60/100
    day1 = [w for w in out if w["id"] == 931013083]
    assert day1 and day1[0]["name"] == "Day 1"
    assert day1[0]["sport"] == "strength_training"


def test_fetch_workouts_single_short_page(monkeypatch):
    def fake_safe(_fn, _path, params=None):
        start = (params or {}).get("start", 0)
        return [{"workoutId": 1, "workoutName": "Day 1",
                 "sportType": {"sportTypeKey": "strength_training"}}] if start == 0 else []

    monkeypatch.setattr(client, "_safe", fake_safe)
    out = client.fetch_workouts()
    assert [w["id"] for w in out] == [1]  # stops after the short first page
