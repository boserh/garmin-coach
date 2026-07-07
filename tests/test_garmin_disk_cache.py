"""PERF-02: the per-key-file Garmin disk cache (immutable assets) — roundtrip,
cross-process visibility via the files, expiry, and the one-time legacy-file seed."""
import json
import os

import pytest

from app.garmin import client


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the client cache at a throwaway dir and clear the in-process memo."""
    d = tmp_path / "gcache"
    monkeypatch.setattr(client, "GARMIN_CACHE_DIR", str(d))
    monkeypatch.setattr(client, "GARMIN_CACHE_FILE", str(tmp_path / "legacy.json"))
    monkeypatch.setattr(client, "_memo", {})
    return d


def test_put_get_roundtrip(cache_dir):
    client._cache_put("series:v1:42", [{"d": 0.1}], ttl_s=60)
    assert client._cache_get("series:v1:42") == [{"d": 0.1}]
    # one file per key, colon-safe name
    assert (cache_dir / "series_v1_42.json").exists()


def test_get_reads_file_when_memo_cold(cache_dir):
    """Cross-process visibility: another process's write (a file) is a hit here."""
    client._cache_put("exercise:v2:7", {"sets": {"SQUAT": 4}}, ttl_s=60)
    client._memo.clear()  # simulate a different process
    assert client._cache_get("exercise:v2:7") == {"sets": {"SQUAT": 4}}


def test_expired_entry_is_a_miss(cache_dir):
    client._cache_put("workout:v2:9", {"name": "x"}, ttl_s=-1)
    assert client._cache_get("workout:v2:9") is None
    client._memo.clear()
    assert client._cache_get("workout:v2:9") is None  # from file too


def test_missing_or_corrupt_file_is_a_miss(cache_dir):
    assert client._cache_get("series:v1:404") is None
    os.makedirs(cache_dir, exist_ok=True)
    (cache_dir / "series_v1_500.json").write_text("{not json", encoding="utf-8")
    assert client._cache_get("series:v1:500") is None


def test_seed_legacy_cache(cache_dir, tmp_path, monkeypatch):
    """The old single-file cache is split into per-key files once (alive entries
    only) and renamed so it never seeds twice."""
    import time
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "series:v1:1": [[{"d": 1.0}], time.time() + 3600],
        "series:v1:2": [[{"d": 2.0}], time.time() - 5],   # expired — not seeded
    }), encoding="utf-8")

    client._seed_legacy_cache()

    assert client._cache_get("series:v1:1") == [{"d": 1.0}]
    assert client._cache_get("series:v1:2") is None
    assert not legacy.exists()
    assert (tmp_path / "legacy.json.migrated").exists()
    # a second call is a no-op (no legacy file anymore)
    client._seed_legacy_cache()


def test_seed_does_not_overwrite_fresher_per_key_file(cache_dir, tmp_path):
    import time
    client._cache_put("workout:v2:5", {"name": "fresh"}, ttl_s=60)
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({
        "workout:v2:5": [{"name": "stale"}, time.time() + 3600],
    }), encoding="utf-8")

    client._seed_legacy_cache()
    client._memo.clear()
    assert client._cache_get("workout:v2:5") == {"name": "fresh"}
