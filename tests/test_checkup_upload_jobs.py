"""Background OCR job processing + websocket broadcast behind /checkups/upload
(batched multi-file, non-blocking upload): `_process_checkup_upload_job` (own DB
session, one Claude call for the whole batch, broadcasts before/after, never raises),
`_broadcast_job` (fan-out to open /checkups/ws tabs, drops dead connections) and
`_prune_upload_jobs` (per-process TTL sweep)."""
import time as time_mod
from unittest.mock import AsyncMock

from app.analysis.service import AnalystError
from app.routers import checkups as checkups_router

U1 = 1


def _job(**kw):
    job = checkups_router.UploadJob(
        id=kw.pop("id", "job1"), user_id=kw.pop("user_id", U1),
        filenames=kw.pop("filenames", ["lab.jpg"]), **kw,
    )
    checkups_router._upload_jobs[job.id] = job
    return job


async def test_process_checkup_upload_job_success(monkeypatch):
    job = _job(id="ocr-ok", filenames=["lab1.jpg", "lab2.jpg"])
    fake_rows = [type("Row", (), {"id": 42})(), type("Row", (), {"id": 43})()]
    monkeypatch.setattr(checkups_router, "run_checkup_ocr_batch", AsyncMock(return_value=fake_rows))
    try:
        await checkups_router._process_checkup_upload_job(
            job.id, [(b"bytes1", "image/jpeg"), (b"bytes2", "image/jpeg")], "2026-07-20", "k")
        assert job.status == "done"
        assert job.checkup_ids == [42, 43]
    finally:
        checkups_router._upload_jobs.pop(job.id, None)


async def test_process_checkup_upload_job_analyst_error(monkeypatch):
    job = _job(id="ocr-err")
    monkeypatch.setattr(
        checkups_router, "run_checkup_ocr_batch", AsyncMock(side_effect=AnalystError("боом")))
    try:
        await checkups_router._process_checkup_upload_job(
            job.id, [(b"bytes", "image/jpeg")], "2026-07-20", "k")
        assert job.status == "error"
        assert job.error == "боом"
    finally:
        checkups_router._upload_jobs.pop(job.id, None)


async def test_process_checkup_upload_job_unexpected_crash_is_caught(monkeypatch):
    """A background job must never raise into the caller (there's no request to catch
    it) — an unexpected exception still lands as a normal error status."""
    job = _job(id="ocr-crash")
    monkeypatch.setattr(
        checkups_router, "run_checkup_ocr_batch", AsyncMock(side_effect=RuntimeError("boom")))
    try:
        await checkups_router._process_checkup_upload_job(
            job.id, [(b"bytes", "image/jpeg")], "2026-07-20", "k")
        assert job.status == "error"
        assert job.error == "Внутрішня помилка."
    finally:
        checkups_router._upload_jobs.pop(job.id, None)


async def test_process_checkup_upload_job_broadcasts_status_over_websocket(monkeypatch):
    job = _job(id="ocr-ws")
    fake_rows = [type("Row", (), {"id": 7})()]
    monkeypatch.setattr(checkups_router, "run_checkup_ocr_batch", AsyncMock(return_value=fake_rows))

    sent = []

    class FakeWs:
        async def send_json(self, payload):
            sent.append(payload)

    checkups_router._ws_by_user[U1] = {FakeWs()}
    try:
        await checkups_router._process_checkup_upload_job(
            job.id, [(b"bytes", "image/jpeg")], "2026-07-20", "k")
    finally:
        checkups_router._ws_by_user.pop(U1, None)
        checkups_router._upload_jobs.pop(job.id, None)

    assert [m["status"] for m in sent] == ["processing", "done"]
    assert sent[-1]["checkup_ids"] == [7]
    assert sent[-1]["job_id"] == "ocr-ws"


async def test_broadcast_job_drops_dead_connections():
    job = checkups_router.UploadJob(id="jobX", user_id=U1, filenames=["x.jpg"])

    class DeadWs:
        async def send_json(self, payload):
            raise RuntimeError("connection closed")

    dead = DeadWs()
    checkups_router._ws_by_user[U1] = {dead}
    try:
        await checkups_router._broadcast_job(job)
        assert dead not in checkups_router._ws_by_user.get(U1, set())
    finally:
        checkups_router._ws_by_user.pop(U1, None)


async def test_broadcast_job_is_noop_with_no_listeners():
    job = checkups_router.UploadJob(id="jobY", user_id=U1, filenames=["y.jpg"])
    checkups_router._ws_by_user.pop(U1, None)
    await checkups_router._broadcast_job(job)  # must not raise


def test_prune_upload_jobs_drops_only_old_finished_jobs():
    old_done = checkups_router.UploadJob(
        id="old1", user_id=U1, filenames=["a.jpg"], status="done")
    old_done.created_at = time_mod.time() - checkups_router.UPLOAD_JOB_TTL_S - 10
    fresh_error = checkups_router.UploadJob(
        id="fresh1", user_id=U1, filenames=["b.jpg"], status="error")
    stuck_processing = checkups_router.UploadJob(
        id="proc1", user_id=U1, filenames=["c.jpg"], status="processing")
    stuck_processing.created_at = time_mod.time() - checkups_router.UPLOAD_JOB_TTL_S - 10

    for j in (old_done, fresh_error, stuck_processing):
        checkups_router._upload_jobs[j.id] = j
    try:
        checkups_router._prune_upload_jobs()
        assert old_done.id not in checkups_router._upload_jobs      # old + finished -> pruned
        assert fresh_error.id in checkups_router._upload_jobs       # finished but recent -> kept
        assert stuck_processing.id in checkups_router._upload_jobs  # in flight -> never pruned
    finally:
        for j in (old_done, fresh_error, stuck_processing):
            checkups_router._upload_jobs.pop(j.id, None)
