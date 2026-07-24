"""ST-16: force-refetch series/splits/exercises in bypass of the immutable-asset disk cache.

The core force behaviour (series/exercises) is already covered in ``test_resync.py``; this
adds the ST-16-specific guards: the empty-response-doesn't-clobber rule, ``force`` on splits,
and the single-key ``cache_del`` helper."""
import pytest

from app.garmin import client


@pytest.fixture(autouse=True)
def _no_limiter(monkeypatch):
    monkeypatch.setattr(client, "_limiter", client._RateLimiter(rps=0))


def test_force_empty_series_does_not_clobber_cache(monkeypatch):
    """A force-refetch that comes back empty (Garmin still computing the track) must NOT
    overwrite a previously-good cached series — keep the old one."""
    good = [{"d": 1.0, "p": 5.0, "hr": 150, "e": None}]
    put_calls = []
    monkeypatch.setattr(client, "_cache_get", lambda k: good)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: put_calls.append(v))

    class P:
        def connectapi(self, path, **kwargs):
            return {"metricDescriptors": [], "activityDetailMetrics": []}   # empty

    monkeypatch.setattr(client, "get_provider", lambda: P())

    out = client.fetch_activity_series(111, sport="running", force=True)
    assert out == good              # returned the preserved cache
    assert put_calls == []          # never wrote the empty result over it


def test_force_empty_splits_does_not_clobber_cache(monkeypatch):
    good = [{"dist_m": 1000.0, "dur_s": 300.0, "pace_min_km": 5.0}]
    put_calls = []
    monkeypatch.setattr(client, "_cache_get", lambda k: good)
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: put_calls.append(v))

    class P:
        def connectapi(self, path, **kwargs):
            return {"lapDTOs": []}   # empty

    monkeypatch.setattr(client, "get_provider", lambda: P())

    out = client.fetch_activity_splits(222, force=True)
    assert out == good
    assert put_calls == []


def test_splits_force_bypasses_cache(monkeypatch):
    """A non-empty force-refetch of splits ignores the (stale) cache and refetches."""
    calls = {"n": 0}
    monkeypatch.setattr(client, "_cache_get", lambda k: [{"dist_m": 1.0, "dur_s": 1.0,
                                                          "pace_min_km": 9.9}])
    monkeypatch.setattr(client, "_cache_put", lambda k, v, ttl: None)

    class P:
        def connectapi(self, path, **kwargs):
            calls["n"] += 1
            return {"lapDTOs": [{"distance": 1000.0, "duration": 300.0,
                                 "averageSpeed": 3.333}]}

    monkeypatch.setattr(client, "get_provider", lambda: P())

    # Default: served from the (stale) cache, no Garmin call.
    assert client.fetch_activity_splits(333)[0]["pace_min_km"] == 9.9
    assert calls["n"] == 0
    # force=True: cache ignored, fresh fetch.
    fresh = client.fetch_activity_splits(333, force=True)
    assert calls["n"] == 1
    assert fresh[0]["dist_m"] == 1000.0


def test_cache_del_removes_memo_and_file(monkeypatch, tmp_path):
    monkeypatch.setattr(client, "GARMIN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(client, "_memo", {})
    key = "series:v2:999"
    client._cache_put(key, [{"d": 1.0}], client.SERIES_TTL_S)
    assert client._cache_get(key) is not None
    import os
    assert os.path.exists(client._key_path(key))

    client.cache_del(key)
    assert client._memo.get(key) is None
    assert not os.path.exists(client._key_path(key))
    assert client._cache_get(key) is None
    # Deleting a missing key is a silent no-op.
    client.cache_del("series:v2:does-not-exist")
