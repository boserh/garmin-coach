"""PERF-04b (dedicated Claude pool + grouped day-fetch) and PERF-05 (Garmin rate
limiter/backoff + per-user fetch lock)."""
import asyncio
import datetime as dt

import pytest

from app.analysis import service as analysis
from app.garmin import client, providers
from app.garmin import service as gservice

# ---------- PERF-04b: dedicated Claude thread pool ----------

def test_run_claude_uses_dedicated_pool():
    """The Claude helper runs the blocking fn on the ``claude`` executor, not the
    shared anyio threadpool (so LLM latency can't starve Garmin work)."""
    import threading

    async def go():
        # _run_claude(fn, *args) calls fn(*args); report the worker thread name.
        return await analysis._run_claude(lambda _: threading.current_thread().name, None)

    name = asyncio.run(go())
    assert name.startswith("claude")


# ---------- PERF-04b: grouped day-fetch ----------

def test_fetch_days_batches_into_one_dict(monkeypatch):
    calls = []
    monkeypatch.setattr(gservice, "daily_summary",
                        lambda d: (calls.append(d) or {"date": d.isoformat()}))
    days = [dt.date(2026, 7, 1), dt.date(2026, 7, 2)]
    out = gservice._fetch_days(days)
    assert set(out) == {"2026-07-01", "2026-07-02"}
    assert calls == days   # each day summarised once, in one hop


# ---------- PERF-05: rate limiter ----------

class _FakeClock:
    """Deterministic monotonic clock — ``sleep`` just advances virtual time."""

    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_rate_limiter_paces_requests(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(client, "_time", clock)
    lim = client._RateLimiter(rps=2.0)   # 0.5s between slots
    for _ in range(5):
        lim.acquire()
    # first slot is free; the next four each wait 0.5s → 2.0s total.
    assert clock.t == pytest.approx(2.0)


def test_rate_limiter_disabled_when_rps_zero(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(client, "_time", clock)
    lim = client._RateLimiter(rps=0)
    for _ in range(10):
        lim.acquire()
    assert clock.t == 0.0   # no throttling


# ---------- PERF-05: 429 recognition + backoff ----------

class _Resp:
    def __init__(self, code):
        self.status_code = code


class _HTTP429(Exception):
    def __init__(self):
        super().__init__("429 Client Error: Too Many Requests")
        self.response = _Resp(429)


def test_is_rate_limited_recognizes_429():
    assert client._is_rate_limited(_HTTP429()) is True
    # garth wraps the requests error under `.error`
    wrapped = Exception("boom")
    wrapped.error = _HTTP429()
    assert client._is_rate_limited(wrapped) is True
    # string fallback
    assert client._is_rate_limited(Exception("HTTP 429 too many requests")) is True
    assert client._is_rate_limited(Exception("500 server error")) is False


def test_api_retries_on_429_then_succeeds(monkeypatch):
    clock = _FakeClock()
    monkeypatch.setattr(client, "_time", clock)
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))
    monkeypatch.setattr(client.settings, "GARMIN_RETRIES", 2)

    attempts = {"n": 0}

    class P:
        def connectapi(self, path, **kw):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                raise _HTTP429()
            return {"ok": True}

    monkeypatch.setattr(client, "get_provider", lambda: P())
    assert client._api("/x") == {"ok": True}
    assert attempts["n"] == 3               # 2 failures + 1 success
    assert clock.t == pytest.approx(1.0 + 2.0)   # backoff 2^0 + 2^1


def test_api_reraises_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(client, "_time", _FakeClock())
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))
    monkeypatch.setattr(client.settings, "GARMIN_RETRIES", 1)

    class P:
        def connectapi(self, path, **kw):
            raise _HTTP429()

    monkeypatch.setattr(client, "get_provider", lambda: P())
    with pytest.raises(_HTTP429):
        client._api("/x")


def test_api_does_not_retry_non_429(monkeypatch):
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))
    monkeypatch.setattr(client.settings, "GARMIN_RETRIES", 3)

    calls = {"n": 0}

    class P:
        def connectapi(self, path, **kw):
            calls["n"] += 1
            raise ValueError("not a rate limit")

    monkeypatch.setattr(client, "get_provider", lambda: P())
    with pytest.raises(ValueError):
        client._api("/x")
    assert calls["n"] == 1   # a non-429 is not retried


# ---------- PERF-05: per-user fetch lock ----------

class _CountingProvider:
    username = "tester"
    display_name = "uuid-1234"

    def __init__(self):
        self.sleep_fetches = 0

    def login(self):
        pass

    def connectapi(self, path, **kwargs):
        if "dailySleepData" in path:
            self.sleep_fetches += 1
            return {"restingHeartRate": 48, "dailySleepDTO": {
                "sleepScores": {"overall": {"value": 82}},
                "deepSleepSeconds": 3600, "lightSleepSeconds": 7200,
                "remSleepSeconds": 5400, "awakeSleepSeconds": 600,
            }}
        if path.startswith("/hrv-service"):
            return {"hrvSummary": {"lastNightAvg": 60, "status": "BALANCED"}}
        if "dailyStress" in path:
            return {"avgStressLevel": 25, "maxStressLevel": 70}
        if "activities/search/activities" in path:
            return []          # no activities → no series fetch / sleep(0.3)
        if "/calendar-service/" in path:
            return {"calendarItems": []}
        return {}


async def test_concurrent_same_user_fetch_runs_once(session, monkeypatch):
    """Two concurrent build_payload_cached for the same user do a single Garmin
    fetch pass — the second waits on the per-user lock, then reuses the fresh
    payload and reports no new activities (avoids double auto-analysis)."""
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))
    # Isolate module-level reuse memo from other tests.
    gservice._recent_payload.clear()

    from app.db.models import User
    user = User(email="u@e.com", password_hash="h")
    session.add(user)
    await session.commit()
    await session.refresh(user)

    fp = _CountingProvider()
    token = providers.set_current_provider(fp)
    try:
        results = await asyncio.gather(
            gservice.build_payload_cached(session, user.id, days=1, activity_limit=5),
            gservice.build_payload_cached(session, user.id, days=1, activity_limit=5),
        )
    finally:
        providers.reset_current_provider(token)

    assert fp.sleep_fetches == 1                       # today fetched once, not twice
    new_counts = sorted(len(new) for _p, new in results)
    # exactly one caller "owns" the freshly-synced activities (here: none), the
    # reuser always gets [].
    assert new_counts[1] == 0 or new_counts == [0, 0]
    assert all(p.synced_today for p, _ in results)
