"""PERF-02: the per-key-file Garmin disk cache (immutable assets) — roundtrip,
cross-process visibility via the files, and expiry."""
import os

import pytest

from app.garmin import client


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the client cache at a throwaway dir and clear the in-process memo."""
    d = tmp_path / "gcache"
    monkeypatch.setattr(client, "GARMIN_CACHE_DIR", str(d))
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


