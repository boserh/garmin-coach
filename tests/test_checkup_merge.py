"""`merge_checkups`: combine 2+ checkups into one — the newest (by date, then id as a
tiebreak) survives and keeps its id; date/title/category/next_due_date come from that
survivor untouched (newest wins), results/notes are combined from all of them
(duplicate result rows skipped), and the other rows are deleted."""
from app.db import checkups as checkups_db

U1 = 1
OTHER_USER = 2


async def _checkup(session, **kw):
    return await checkups_db.create_checkup(
        session, kw.pop("user_id", U1),
        date=kw.pop("date", "2026-07-15"), title=kw.pop("title", "Кров"), **kw)


async def test_merge_survivor_is_the_newest_and_keeps_its_id(session):
    older = await _checkup(session, date="2026-06-01", title="Стара назва", category="старе")
    newer = await _checkup(session, date="2026-07-15", title="Нова назва", category="нове",
                           next_due_date="2027-01-01")

    survivor = await checkups_db.merge_checkups(session, U1, [older.id, newer.id])

    assert survivor.id == newer.id
    assert survivor.date == "2026-07-15"
    assert survivor.title == "Нова назва"
    assert survivor.category == "нове"
    assert survivor.next_due_date == "2027-01-01"


async def test_merge_combines_results_and_notes_from_both(session):
    older = await _checkup(
        session, date="2026-06-01",
        results=[{"name": "Феритин", "value": "60", "unit": "нг/мл", "ref_range": "30-400"}],
        notes="перший запис",
    )
    newer = await _checkup(
        session, date="2026-07-15",
        results=[{"name": "ТТГ", "value": "2.0", "unit": "мОд/л", "ref_range": "0.4-4.0"}],
        notes="другий запис",
    )

    survivor = await checkups_db.merge_checkups(session, U1, [older.id, newer.id])

    names = {r["name"] for r in survivor.results}
    assert names == {"Феритин", "ТТГ"}
    assert survivor.notes == "перший запис\n\nдругий запис"  # oldest -> newest order


async def test_merge_skips_exact_duplicate_result_rows(session):
    row = {"name": "Феритин", "value": "45", "unit": "нг/мл", "ref_range": "30-400"}
    older = await _checkup(session, date="2026-06-01", results=[row])
    newer = await _checkup(session, date="2026-07-15", results=[dict(row)])

    survivor = await checkups_db.merge_checkups(session, U1, [older.id, newer.id])

    assert survivor.results == [row]


async def test_merge_clears_stale_analysis(session):
    older = await _checkup(session, date="2026-06-01")
    newer = await _checkup(session, date="2026-07-15")
    await checkups_db.set_analysis(session, newer, "стара інтерпретація")

    survivor = await checkups_db.merge_checkups(session, U1, [older.id, newer.id])

    assert survivor.analysis is None


async def test_merge_deletes_the_other_rows(session):
    older = await _checkup(session, date="2026-06-01")
    newer = await _checkup(session, date="2026-07-15")

    await checkups_db.merge_checkups(session, U1, [older.id, newer.id])

    remaining = await checkups_db.list_checkups(session, U1)
    assert [r.id for r in remaining] == [newer.id]


async def test_merge_same_date_breaks_tie_on_id(session):
    first = await _checkup(session, date="2026-07-15", title="Перший")
    second = await _checkup(session, date="2026-07-15", title="Другий")  # higher id, same date

    survivor = await checkups_db.merge_checkups(session, U1, [first.id, second.id])

    assert survivor.id == second.id


async def test_merge_returns_none_with_fewer_than_two_owned_ids(session):
    only = await _checkup(session)
    other_users_checkup = await _checkup(session, user_id=OTHER_USER, date="2026-07-01")

    assert await checkups_db.merge_checkups(session, U1, [only.id]) is None
    # the second id belongs to another user -> silently dropped -> still just 1 owned
    assert await checkups_db.merge_checkups(
        session, U1, [only.id, other_users_checkup.id]) is None


async def test_merge_ignores_ids_not_owned_by_user(session):
    mine1 = await _checkup(session, date="2026-06-01", title="Моє 1")
    mine2 = await _checkup(session, date="2026-07-15", title="Моє 2")
    others = await _checkup(session, user_id=OTHER_USER, date="2026-07-20", title="Не моє")

    survivor = await checkups_db.merge_checkups(session, U1, [mine1.id, mine2.id, others.id])

    assert survivor.id == mine2.id  # others.id silently dropped, doesn't even win "newest"
    remaining_ids = {r.id for r in await checkups_db.list_checkups(session, OTHER_USER)}
    assert others.id in remaining_ids  # untouched
