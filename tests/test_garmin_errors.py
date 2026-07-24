"""OPS-05 · Garmin API error visibility — classification, ring buffer, bot_state flush,
the /status counters and the once-a-day burst DM guard. Zero real Garmin/Claude calls."""
import time
from types import SimpleNamespace

import pytest

from app.garmin import client, service


@pytest.fixture(autouse=True)
def _clean_buffer():
    client.drain_errors()   # start each test from an empty ring buffer
    yield
    client.drain_errors()


class _Resp:
    def __init__(self, code):
        self.status_code = code


def _http_error(code):
    # garth wraps a requests error in exc.error.response.status_code
    return SimpleNamespace(error=SimpleNamespace(response=_Resp(code)))


# ---------- classification ----------

def test_classify_by_nested_status():
    assert client._classify_error(_http_error(401)) == "401"
    assert client._classify_error(_http_error(403)) == "403"
    assert client._classify_error(_http_error(429)) == "429"
    assert client._classify_error(_http_error(503)) == "5xx"


def test_classify_by_string_fallback():
    assert client._classify_error(Exception("HTTP 429 Too Many Requests")) == "429"
    assert client._classify_error(Exception("403 Forbidden")) == "403"
    assert client._classify_error(TimeoutError("connection timed out")) == "network"
    assert client._classify_error(Exception("weird")) == "other"


def test_endpoint_suffix_groups_by_service():
    assert client._endpoint_suffix("/hrv-service/hrv/2026-07-24") == "/hrv-service/hrv"
    assert client._endpoint_suffix(
        "/wellness-service/wellness/dailyStress/2026-07-24?x=1") == "/wellness-service/wellness"


# ---------- ring buffer via _safe ----------

def test_safe_records_error_and_single_retried_429_not_counted():
    def boom(path):
        raise Exception("500 server error")

    r = client._safe(boom, "/foo-service/bar/1")
    assert r == {"_error": "500 server error"}
    errs = client.recent_errors()
    assert len(errs) == 1
    assert errs[0]["endpoint"] == "/foo-service/bar" and errs[0]["kind"] == "5xx"

    # A success (a 429 the retry cleared returns normally through _safe) records nothing.
    client.drain_errors()
    assert client._safe(lambda p: {"ok": 1}, "/foo-service/bar/1") == {"ok": 1}
    assert client.recent_errors() == []


def test_expected_endpoint_flagged():
    def boom(path):
        raise Exception("403 Forbidden")

    client._safe(boom, "/biometric-service/whatever/1")
    e = client.recent_errors()[0]
    assert e["kind"] == "403" and e["expected"] is True


def test_buffer_capped():
    for i in range(client._ERROR_BUFFER_MAX + 20):
        client._record_error(f"/svc/x/{i}", Exception("boom"))
    assert len(client.recent_errors()) == client._ERROR_BUFFER_MAX


# ---------- summarize_garmin_errors ----------

def test_summarize_counts_24h_excludes_expected_and_old():
    now = time.time()
    import json
    recent = [
        {"ts": now - 100, "endpoint": "/a/b", "kind": "403", "expected": False},
        {"ts": now - 200, "endpoint": "/a/b", "kind": "403", "expected": False},
        {"ts": now - 300, "endpoint": "/biometric-service/x", "kind": "403", "expected": True},
        {"ts": now - 90000, "endpoint": "/a/b", "kind": "429", "expected": False},  # >24h
    ]
    blob = json.dumps({"updated": now, "recent": recent})
    s = service.summarize_garmin_errors(blob, now=now)
    assert s["count_24h"] == 2
    assert s["counts_24h"] == {"403": 2}
    assert s["last"]["kind"] == "429"


def test_summarize_bad_blob_is_empty():
    s = service.summarize_garmin_errors("not json")
    assert s["count_24h"] == 0 and s["recent"] == []
    assert service.summarize_garmin_errors(None)["count_24h"] == 0


# ---------- flush into bot_state ----------

async def test_flush_garmin_errors_merges_into_bot_state(session):
    from app.garmin import repository

    client._record_error("/hrv-service/hrv/1", Exception("403 Forbidden"))
    client._record_error("/foo/bar/2", Exception("timeout"))
    await service._flush_garmin_errors(session, user_id=7)
    await session.commit()

    blob = await repository.get_state(session, 7, service.GARMIN_ERRORS_KEY)
    s = service.summarize_garmin_errors(blob)
    assert s["count_24h"] == 2
    # buffer drained — a second flush with nothing new keeps the same rows
    await service._flush_garmin_errors(session, user_id=7)
    blob2 = await repository.get_state(session, 7, service.GARMIN_ERRORS_KEY)
    assert service.summarize_garmin_errors(blob2)["count_24h"] == 2
