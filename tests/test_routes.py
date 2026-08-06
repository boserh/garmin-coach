"""NF-33: route fingerprinting, recognition and the honest comparison of repeats.

The privacy AC ("coordinates never leave the Pi") is tested here too, against the real
``activity_payload`` builder — a regression there would leak a home address into a prompt and
a ``report_logs`` row, which is the whole risk of this feature.
"""
import json
import math

import pytest

from app import routes as routes_mod
from app.db.models import ActivityRecord, Route, User


def _loop(n=40, *, lat0=50.0500, lon0=19.9400, radius_deg=0.006, elev_amp=30.0,
          reverse=False, scale=1.0, step_km=0.25):
    """A synthetic circular route with a single hill, as a stored series would look."""
    pts = []
    for i in range(n):
        frac = i / (n - 1)
        ang = 2 * math.pi * (1 - frac if reverse else frac)
        # elevation follows the position on the loop, so running it backwards mirrors it
        elev = 100.0 + elev_amp * math.sin(ang)
        pts.append({
            "d": round(i * step_km * scale, 2),
            "p": 6.0,
            "hr": 145,
            "e": round(elev, 1),
            "lat": round(lat0 + radius_deg * math.sin(ang), 6),
            "lon": round(lon0 + radius_deg * math.cos(ang), 6),
        })
    return pts


# ---------- fingerprints ----------

def test_no_gps_no_fingerprint():
    """A treadmill run / an old pre-NF-33 series has no coordinates: route_id stays None and
    every section stays silent (an AC)."""
    treadmill = [{"d": i * 0.1, "p": 5.5, "hr": 150, "e": None} for i in range(60)]
    assert routes_mod.fingerprint(treadmill) is None
    assert routes_mod.fingerprint(None) is None
    assert routes_mod.fingerprint([]) is None


def test_fingerprint_is_small_and_carries_no_track():
    fp = routes_mod.fingerprint(_loop())
    assert routes_mod.fingerprint_bytes(fp) <= 1024
    # It is a signature, not a track: only a coarsened start point survives.
    assert set(fp) <= {"start", "dist_km", "gain_m", "profile", "bearings"}
    assert len(fp["start"]) == 2
    # Coarsened to ~110 m — a neighbourhood, not a doorstep.
    assert fp["start"][0] == round(fp["start"][0], routes_mod.START_PRECISION)


def test_a_short_jog_is_not_a_route():
    assert routes_mod.fingerprint(_loop(n=12, step_km=0.05)) is None


# ---------- recognition ----------

def test_the_same_loop_is_recognised_despite_gps_drift():
    a = routes_mod.fingerprint(_loop())
    # ~20 m of city drift + a slightly different stopping point
    b = routes_mod.fingerprint(_loop(lat0=50.05018, lon0=19.94015, scale=1.02))
    assert routes_mod.similar(a, b) is True


def test_a_different_loop_is_not_the_same_route():
    a = routes_mod.fingerprint(_loop())
    far = routes_mod.fingerprint(_loop(lat0=50.2000, lon0=20.2000))
    assert routes_mod.similar(a, far) is False
    longer = routes_mod.fingerprint(_loop(scale=1.5))     # same start, 50% longer
    assert routes_mod.similar(a, longer) is False


def test_the_same_loop_run_backwards_is_a_different_route():
    """Deliberate (an AC): the climb is in the other half, so comparing the two passes would
    be dishonest — better two routes than one lying comparison."""
    forward = routes_mod.fingerprint(_loop())
    backward = routes_mod.fingerprint(_loop(reverse=True))
    assert routes_mod.similar(forward, backward) is False


def test_matching_is_first_match_so_clustering_is_idempotent():
    fp = routes_mod.fingerprint(_loop())
    near = routes_mod.fingerprint(_loop(lat0=50.05012))
    assert routes_mod.match(fp, [(7, near), (9, near)]) == 7
    assert routes_mod.match(fp, []) is None
    assert routes_mod.match(None, [(7, near)]) is None


# ---------- comparing the repeats ----------

def test_first_pass_has_nothing_to_compare():
    assert routes_mod.build_comparison({"gap_pace_min_km": 5.4}, []) is None


def test_comparison_uses_gap_pace_and_reports_both_deltas():
    history = [
        {"date": "2026-05-01", "gap_pace_min_km": 5.60, "avg_hr": 150},
        {"date": "2026-06-01", "gap_pace_min_km": 5.30, "avg_hr": 148},   # the best
        {"date": "2026-07-01", "gap_pace_min_km": 5.50, "avg_hr": 149},   # the previous
    ]
    c = routes_mod.build_comparison(
        {"date": "2026-08-01", "gap_pace_min_km": 5.40, "avg_hr": 145}, history)
    assert c["run_number"] == 4
    assert c["best_gap_pace"] == 5.30 and c["best_date"] == "2026-06-01"
    assert c["prev_gap_pace"] == 5.50 and c["prev_date"] == "2026-07-01"
    assert c["delta_prev_s"] == -6      # 0.1 min/km faster than last time
    assert c["delta_best_s"] == 6       # still 6 s/km off the best
    text = routes_mod.summary(c, "парк")
    assert "парк" in text and "4-те проходження" in text
    assert "145" in text and "149" in text


def test_summary_is_silent_without_a_previous_pace():
    c = routes_mod.build_comparison({"gap_pace_min_km": None},
                                    [{"date": "2026-07-01", "gap_pace_min_km": None}])
    assert routes_mod.summary(c) is None


# ---------- storage: clustering + the privacy rule ----------

async def _user(session, email="routes@example.com"):
    u = User(email=email, password_hash="x", is_active=True, is_admin=False)
    session.add(u)
    await session.flush()
    return u


async def _activity(session, user_id, activity_id, series, *, date="2026-08-01",
                    dur_min=60.0, dist_km=10.0, avg_hr=145):
    a = ActivityRecord(user_id=user_id, activity_id=activity_id, date=date, type="running",
                       dur_min=dur_min, dist_km=dist_km, avg_hr=avg_hr, series=series)
    session.add(a)
    await session.flush()
    return a


@pytest.mark.asyncio
async def test_clustering_is_idempotent_and_user_scoped(session):
    from app.garmin.repository import routes as routes_repo

    user = await _user(session)
    other = await _user(session, "someone-else@example.com")

    a1 = await _activity(session, user.id, 1, _loop())
    a2 = await _activity(session, user.id, 2, _loop(lat0=50.05012), date="2026-08-08")
    foreign = await _activity(session, other.id, 3, _loop())

    r1 = await routes_repo.assign_route(session, user.id, a1)
    r2 = await routes_repo.assign_route(session, user.id, a2)
    assert r1 is not None and r1 == r2, "the same loop must land in one cluster"

    # Re-running the backfill cannot duplicate routes or re-partition history (an AC).
    assert await routes_repo.assign_route(session, user.id, a1) == r1
    assert len(await routes_repo.list_routes(session, user.id)) == 1

    # Another account's identical loop is a route of THEIRS, never a shared cluster.
    r_other = await routes_repo.assign_route(session, other.id, foreign)
    assert r_other != r1
    assert len(await routes_repo.list_routes(session, user.id)) == 1
    assert await routes_repo.get_route(session, user.id, r_other) is None


@pytest.mark.asyncio
async def test_route_context_compares_passes_and_never_leaks_coordinates(session):
    """The central privacy AC: what reaches Claude (and therefore ``report_logs``) is an
    anonymised route_id plus numbers — never a coordinate, in any nested field."""
    from app.analysis.reports import activity_payload
    from app.garmin.repository import routes as routes_repo

    user = await _user(session, "privacy@example.com")
    first = await _activity(session, user.id, 11, _loop(), date="2026-07-01", dur_min=62.0)
    second = await _activity(session, user.id, 12, _loop(lat0=50.05011),
                             date="2026-08-01", dur_min=58.0)
    await routes_repo.assign_route(session, user.id, first)
    await routes_repo.assign_route(session, user.id, second)

    ctx = await routes_repo.build_route_context(session, user.id, second)
    assert ctx["run_number"] == 2
    assert ctx["delta_prev_s"] < 0            # 4 minutes quicker over the same loop
    assert "lat" not in json.dumps(ctx) and "lon" not in json.dumps(ctx)

    payload = json.dumps(activity_payload(second, None, ctx), ensure_ascii=False)
    for banned in ('"lat"', '"lon"', "50.05", "19.94"):
        assert banned not in payload, "coordinates must never enter an LLM context"


@pytest.mark.asyncio
async def test_first_pass_produces_no_route_context(session):
    from app.garmin.repository import routes as routes_repo

    user = await _user(session, "firstpass@example.com")
    a = await _activity(session, user.id, 21, _loop())
    await routes_repo.assign_route(session, user.id, a)
    assert await routes_repo.build_route_context(session, user.id, a) is None


@pytest.mark.asyncio
async def test_rename_is_user_scoped(session):
    from app.garmin.repository import routes as routes_repo

    user = await _user(session, "rename@example.com")
    other = await _user(session, "rename-other@example.com")
    a = await _activity(session, user.id, 31, _loop())
    route_id = await routes_repo.assign_route(session, user.id, a)

    assert await routes_repo.rename_route(session, user.id, route_id, "  парк  ") is True
    assert (await routes_repo.get_route(session, user.id, route_id)).name == "парк"
    assert await routes_repo.rename_route(session, other.id, route_id, "чуже") is False


@pytest.mark.asyncio
async def test_hidden_passes_are_excluded(session):
    """ST-17 hidden activities are excluded from every aggregate — a broken-GPS duplicate
    must not become the "best" pass of a route."""
    from app.garmin.repository import routes as routes_repo

    user = await _user(session, "hidden@example.com")
    a1 = await _activity(session, user.id, 41, _loop(), date="2026-07-01")
    a2 = await _activity(session, user.id, 42, _loop(), date="2026-07-08", dur_min=20.0)
    a2.is_hidden = True
    route_id = await routes_repo.assign_route(session, user.id, a1)
    a2.route_id = route_id
    await session.flush()
    passes = await routes_repo.route_passes(session, user.id, route_id)
    assert [p["date"] for p in passes] == ["2026-07-01"]


@pytest.mark.asyncio
async def test_sync_assigns_routes_to_freshly_stored_activities(session):
    """Route recognition happens as part of the sync, so "is this my usual loop?" is
    answerable the moment a run lands — not only after a manual backfill."""
    from app.garmin.repository import routes as routes_repo

    user = await _user(session, "sync@example.com")
    a = await _activity(session, user.id, 51, _loop())
    assert a.route_id is None
    linked = await routes_repo.assign_routes_for_activities(session, user.id, [51])
    assert linked == 1
    assert a.route_id is not None
    # Idempotent: a second pass over the same ids links nothing new.
    assert await routes_repo.assign_routes_for_activities(session, user.id, [51]) == 0


@pytest.mark.asyncio
async def test_routes_table_is_registered_for_the_orm(session):
    """Guards the migration/model pairing: the table must exist in the test schema, which is
    built from the models — a mismatch with alembic shows up as a missing column at runtime."""
    session.add(Route(user_id=1, fingerprint={"start": [50.0, 19.0], "dist_km": 5.0}))
    await session.flush()
