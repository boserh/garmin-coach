"""ST-20: /admin/cache page — llm_cache stats/purge + Garmin disk-cache stats/purge."""
import time

import anyio
import pytest

from app.db import llm_cache
from app.db.base import async_session_maker
from app.db.models import LlmCache
from app.garmin import client

# ---------- app/db/llm_cache.py: stats/purge_expired/purge_all ----------

async def test_llm_cache_stats_counts_total_and_expired(session):
    await llm_cache.put(session, "alive", "звіт", ttl_s=60)
    session.add(LlmCache(key="dead", value="старий", expires_at=time.time() - 5))
    await session.commit()
    s = await llm_cache.stats(session)
    assert s["total"] == 2
    assert s["expired"] == 1
    assert s["bytes"] > 0


async def test_llm_cache_purge_expired_only_removes_expired(session):
    await llm_cache.put(session, "alive", "звіт", ttl_s=60)
    session.add(LlmCache(key="dead", value="старий", expires_at=time.time() - 5))
    await session.commit()
    n = await llm_cache.purge_expired(session)
    assert n == 1
    assert await llm_cache.get(session, "alive") == "звіт"
    s = await llm_cache.stats(session)
    assert s["total"] == 1


async def test_llm_cache_purge_all_wipes_everything(session):
    await llm_cache.put(session, "a", "1", ttl_s=60)
    await llm_cache.put(session, "b", "2", ttl_s=60)
    n = await llm_cache.purge_all(session)
    assert n == 2
    s = await llm_cache.stats(session)
    assert s["total"] == 0


# ---------- app/garmin/client.py: cache_stats/cache_purge_expired/cache_del_activity ----------

@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "gcache"
    monkeypatch.setattr(client, "GARMIN_CACHE_DIR", str(d))
    monkeypatch.setattr(client, "_memo", {})
    return d


def test_cache_stats_groups_by_prefix(cache_dir):
    client._cache_put("series:v2:1", [{"d": 0.1}], ttl_s=60)
    client._cache_put("series:v2:2", [{"d": 0.2}], ttl_s=60)
    client._cache_put("exercise:v3:1", {"sets": {}}, ttl_s=60)
    client._cache_put("gear_stats:v1:abc", {"km": 5}, ttl_s=60)
    stats = client.cache_stats()
    assert stats["by_prefix"]["series_v2"]["count"] == 2
    assert stats["by_prefix"]["exercise_v3"]["count"] == 1
    assert stats["by_prefix"]["gear_stats_v1"]["count"] == 1  # multi-word prefix, not merged
    assert stats["total_files"] == 4
    assert stats["total_bytes"] > 0


def test_cache_purge_expired_removes_only_expired_files(cache_dir):
    client._cache_put("series:v2:1", [{"d": 0.1}], ttl_s=-1)   # expired
    client._cache_put("series:v2:2", [{"d": 0.2}], ttl_s=60)   # alive
    n = client.cache_purge_expired()
    assert n == 1
    client._memo.clear()
    assert client._cache_get("series:v2:1") is None
    assert client._cache_get("series:v2:2") == [{"d": 0.2}]


def test_cache_del_activity_removes_only_that_activitys_keys(cache_dir):
    client._cache_put("series:v2:42", [{"d": 1}], ttl_s=60)
    client._cache_put("splits:v1:42", [{"lap": 1}], ttl_s=60)
    client._cache_put("exercise:v3:42", {"sets": {}}, ttl_s=60)
    client._cache_put("gear_link:v1:42", [{"gearPk": "x"}], ttl_s=60)
    client._cache_put("series:v2:99", [{"d": 2}], ttl_s=60)  # a different activity

    client.cache_del_activity(42)
    client._memo.clear()
    assert client._cache_get("series:v2:42") is None
    assert client._cache_get("splits:v1:42") is None
    assert client._cache_get("exercise:v3:42") is None
    assert client._cache_get("gear_link:v1:42") is None
    assert client._cache_get("series:v2:99") == [{"d": 2}]


# ---------- /admin/cache router ----------

def test_admin_cache_requires_admin(client):
    r = client.get("/admin/cache", follow_redirects=False)
    assert r.status_code == 303


def test_admin_cache_page_renders(auth_client):
    r = auth_client.get("/admin/cache")
    assert r.status_code == 200
    assert "llm_cache" in r.text.lower() or "Claude dedup" in r.text


def test_admin_cache_llm_purge_expired_action(auth_client):
    async def seed():
        async with async_session_maker() as s:
            await llm_cache.put(s, "dead", "x", ttl_s=-1)

    anyio.run(seed)
    r = auth_client.post("/admin/cache/llm/purge_expired", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin/cache?msg=")


def test_admin_cache_garmin_del_activity_action(auth_client, cache_dir):
    client._cache_put("series:v2:7", [{"d": 1}], ttl_s=60)
    r = auth_client.post(
        "/admin/cache/garmin/del_activity", data={"activity_id": "7"}, follow_redirects=False
    )
    assert r.status_code == 303
    client._memo.clear()
    assert client._cache_get("series:v2:7") is None
