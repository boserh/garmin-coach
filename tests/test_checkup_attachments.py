"""`CheckupAttachment` DB layer: `add_attachment`/`list_attachments`/`get_attachment`
(scoped by checkup_id so one checkup's attachment id can't serve another's file) and
`delete_checkup` cascading its attachments (no DB-level FK cascade is configured in
this codebase, so it's done explicitly)."""
from app.db import checkups as checkups_db

U1 = 1


async def _checkup(session, **kw):
    return await checkups_db.create_checkup(
        session, U1, date=kw.pop("date", "2026-07-15"), title=kw.pop("title", "Кров"), **kw)


async def test_add_and_list_attachments(session):
    c = await _checkup(session)
    a1 = await checkups_db.add_attachment(
        session, c.id, filename="lab1.jpg", media_type="image/jpeg", data=b"bytes1")
    a2 = await checkups_db.add_attachment(
        session, c.id, filename="lab2.pdf", media_type="application/pdf", data=b"bytes2")

    rows = await checkups_db.list_attachments(session, c.id)
    assert [r.id for r in rows] == [a1.id, a2.id]
    assert rows[0].filename == "lab1.jpg" and rows[0].data == b"bytes1"
    assert rows[1].media_type == "application/pdf"


async def test_get_attachment_scoped_to_checkup(session):
    c1 = await _checkup(session, title="Перший")
    c2 = await _checkup(session, title="Другий")
    a = await checkups_db.add_attachment(
        session, c1.id, filename="lab.jpg", media_type="image/jpeg", data=b"bytes")

    assert await checkups_db.get_attachment(session, c1.id, a.id) is not None
    # same attachment id, wrong checkup -> None (can't guess another checkup's file)
    assert await checkups_db.get_attachment(session, c2.id, a.id) is None


async def test_get_attachment_missing_id_returns_none(session):
    c = await _checkup(session)
    assert await checkups_db.get_attachment(session, c.id, 999999) is None


async def test_delete_checkup_cascades_attachments(session):
    c = await _checkup(session)
    a = await checkups_db.add_attachment(
        session, c.id, filename="lab.jpg", media_type="image/jpeg", data=b"bytes")

    await checkups_db.delete_checkup(session, c)

    assert await checkups_db.get_attachment(session, c.id, a.id) is None
