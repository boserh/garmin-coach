#!/usr/bin/env python3
"""OPS-01 recon: validate `python-garminconnect` as the garth replacement ("plan B").

Runs a real login (incl. MFA) and exercises every Garmin endpoint this project
uses (see app/garmin/client.py), printing a PASS/FAIL table to paste into
docs/backlog/OPS-01-garmin-auth-plan-b.md.

Deliberately standalone — NO app imports — so it runs in a throwaway venv with
the *latest* python-garminconnect. Do NOT run it in the project venv: that one
pins garth==0.4.47 (the working production path) and its garminconnect 0.2.8
authenticates *through* that same old garth, which proves nothing about the
Cloudflare-era auth engine. Setup:

    python3 -m venv /tmp/ops01-venv
    /tmp/ops01-venv/bin/pip install --upgrade garminconnect
    GARMIN_EMAIL=... GARMIN_PASSWORD=... \
        /tmp/ops01-venv/bin/python scripts/ops01_recon_gconn.py

Tokens are saved to an isolated dir (default ./.ops01_tokens — gitignored);
the script never touches ~/.garth or the app DB. A fresh login mints a new
OAuth1 token on Garmin's side but does not invalidate existing ones (verified
2026-07-06: production kept working after a parallel full login).

Options:
    --token-dir DIR   where to resume/save this script's own tokens
    --date YYYY-MM-DD day to query (default: yesterday — today may be unsynced)
    --write-test      also round-trip a workout (create → schedule → delete)
"""
import argparse
import datetime as dt
import getpass
import inspect
import os
import sys
import traceback

RESULTS = []  # (name, status, note)


def record(name, status, note=""):
    RESULTS.append((name, status, note))
    print(f"  [{status:>4}] {name}{'  — ' + note if note else ''}")


def report_versions():
    print("== environment ==")
    print(f"  python          {sys.version.split()[0]}")
    for mod in ("garminconnect", "garth", "curl_cffi", "requests"):
        try:
            m = __import__(mod)
            ver = getattr(m, "__version__", None)
            if ver is None:
                try:
                    from importlib.metadata import version
                    ver = version(mod)
                except Exception:
                    ver = "?"
            print(f"  {mod:<15} {ver}")
        except ImportError:
            print(f"  {mod:<15} not installed")
    # The whole point of plan B is escaping garth's blocked SSO flow. If the
    # installed garminconnect still logs in *via* garth, say so loudly.
    try:
        import garminconnect
        src = inspect.getsource(garminconnect)
        engine = "garth-based" if "garth" in src.split("class Garmin")[0] else "native"
        print(f"  auth engine     {engine} (heuristic: imports in garminconnect/__init__.py)")
    except Exception:
        pass
    print()


def do_login(email, password, token_dir):
    """Login handling both old (0.2.8-era) and current python-garminconnect APIs.
    Returns a logged-in Garmin instance."""
    from garminconnect import Garmin

    # 1. Resume from this script's own token dir if a previous run saved one.
    if os.path.isdir(token_dir) and os.listdir(token_dir):
        try:
            api = Garmin()
            api.login(token_dir)
            record("login: token resume", "PASS", token_dir)
            return api
        except Exception as e:
            record("login: token resume", "FAIL", f"{type(e).__name__}: {e} — trying fresh")

    # 2. Fresh login. Newer versions take prompt_mfa / return_on_mfa kwargs.
    params = inspect.signature(Garmin.__init__).parameters
    kwargs = {}
    if "prompt_mfa" in params:
        kwargs["prompt_mfa"] = lambda: input("MFA code: ").strip()
    api = Garmin(email=email, password=password, **kwargs)
    try:
        result = api.login()
        # Current API: with return_on_mfa the result is ("needs_mfa", state).
        if isinstance(result, tuple) and result and result[0] == "needs_mfa":
            code = input("MFA code: ").strip()
            api.resume_login(result[1], code)
        record("login: fresh email+password", "PASS", "MFA prompted" if kwargs else "")
    except Exception as e:
        record("login: fresh email+password", "FAIL", f"{type(e).__name__}: {e}")
        raise

    save_tokens(api, token_dir)
    return api


def save_tokens(api, token_dir):
    """Persist the session. The save API moved between versions (0.2.x had an
    internal `api.garth`; 0.3.6 is fully native) — try the known spellings and,
    failing that, report the candidates so the run itself documents the API."""
    os.makedirs(token_dir, exist_ok=True)
    for chain in ("garth.dump", "client.dump", "dump_tokens", "save_tokens", "dump", "save"):
        obj = api
        try:
            for part in chain.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            continue
        try:
            obj(token_dir)
            record("login: token save", "PASS", f"{token_dir} via api.{chain}")
            return
        except Exception as e:
            record("login: token save", "FAIL", f"api.{chain}: {type(e).__name__}: {e}")
            return
    cands = sorted(
        a for a in dir(api) if not a.startswith("_")
        and any(k in a.lower() for k in ("token", "dump", "save", "session", "auth"))
    )
    record("login: token save", "FAIL", f"no known save API; candidates: {cands}")


def get_identity(api):
    """(userName, displayName, how). Old versions expose them on `api.garth.profile`;
    on native versions ask Garmin itself — which also proves connectapi works."""
    garth_client = getattr(api, "garth", None)
    if garth_client is not None:
        prof = garth_client.profile
        return prof["userName"], prof["displayName"], "api.garth.profile"
    prof = api.connectapi("/userprofile-service/socialProfile")
    return prof["userName"], prof["displayName"], "socialProfile endpoint"


def check(api, name, path, params=None):
    """GET one connectapi endpoint; classify PASS / EMPTY / FAIL. Returns the data."""
    try:
        r = api.connectapi(path, params=params)
    except Exception as e:
        record(name, "FAIL", f"{type(e).__name__}: {e}")
        return None
    if r in (None, [], {}):
        record(name, "EMPTY", "no data for this day — endpoint itself reachable")
    else:
        size = len(r) if isinstance(r, (list, dict)) else 1
        record(name, "PASS", f"{type(r).__name__}[{size}]")
    return r


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--token-dir", default="./.ops01_tokens")
    ap.add_argument("--date", help="day to query, YYYY-MM-DD (default: yesterday)")
    ap.add_argument("--write-test", action="store_true",
                    help="also create+schedule+delete a test workout")
    args = ap.parse_args()

    day = (dt.date.fromisoformat(args.date) if args.date
           else dt.date.today() - dt.timedelta(days=1))

    report_versions()

    email = os.environ.get("GARMIN_EMAIL") or input("Garmin email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass("Garmin password: ")

    print("== login ==")
    try:
        api = do_login(email, password, args.token_dir)
    except Exception:
        traceback.print_exc()
        summary()
        return 1

    # Profile identifiers — the sleep/summary endpoints are keyed by them.
    try:
        username, display_name, how = get_identity(api)
        record("profile: userName/displayName", "PASS", f"{display_name} (via {how})")
    except Exception as e:
        record("profile: userName/displayName", "FAIL", f"{type(e).__name__}: {e}")
        summary()
        return 1

    d = day.isoformat()
    print(f"\n== daily endpoints ({d}) ==")
    check(api, "sleep", f"/wellness-service/wellness/dailySleepData/{username}",
          {"date": d, "nonSleepBufferMinutes": 60})
    check(api, "hrv (hrvService)", f"/hrv-service/hrv/{d}")
    check(api, "stress", f"/wellness-service/wellness/dailyStress/{d}")
    check(api, "body battery", "/wellness-service/wellness/bodyBattery/reports/daily",
          {"startDate": d, "endDate": d})
    check(api, "training readiness", f"/metrics-service/metrics/trainingreadiness/{d}")
    check(api, "user summary", f"/usersummary-service/usersummary/daily/{display_name}",
          {"calendarDate": d})
    check(api, "vo2max (maxmet)", f"/metrics-service/metrics/maxmet/daily/{d}/{d}")
    check(api, "race predictions",
          f"/metrics-service/metrics/racepredictions/latest/{display_name}")
    check(api, "endurance score", "/metrics-service/metrics/endurancescore",
          {"calendarDate": d})
    check(api, "daily events", "/wellness-service/wellness/dailyEvents",
          {"calendarDate": d})

    print("\n== activities ==")
    acts = check(api, "activities list",
                 "/activitylist-service/activities/search/activities",
                 {"start": 0, "limit": 10}) or []
    run = next((a for a in acts
                if "running" in str(a.get("activityType", {}).get("typeKey", ""))), None)
    if run:
        aid = run["activityId"]
        check(api, f"activity details/series (id {aid})",
              f"/activity-service/activity/{aid}/details", {"maxChartSize": 500})
    else:
        record("activity details/series", "SKIP", "no recent run in the last 10 activities")
    strength = next((a for a in acts
                     if "strength" in str(a.get("activityType", {}).get("typeKey", ""))), None)
    if strength:
        aid = strength["activityId"]
        check(api, f"exerciseSets (id {aid})",
              f"/activity-service/activity/{aid}/exerciseSets")
    else:
        record("exerciseSets", "SKIP", "no recent strength activity")

    print("\n== calendar / workouts ==")
    check(api, "calendar month", f"/calendar-service/year/{day.year}/month/{day.month - 1}")
    workouts = check(api, "workouts list", "/workout-service/workouts",
                     {"start": 0, "limit": 10}) or []
    if workouts:
        wid = workouts[0].get("workoutId")
        check(api, f"workout full (id {wid})", f"/workout-service/workout/{wid}")
    else:
        record("workout full", "SKIP", "no saved workouts")

    if args.write_test:
        print("\n== write round-trip (create → schedule → delete) ==")
        write_roundtrip(api, day)

    summary()
    return 0


def write_roundtrip(api, day):
    """Prove the plan-push path: POST a tiny workout, schedule it, remove both."""
    payload = {
        "workoutName": "OPS-01 recon (safe to delete)",
        "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
        "workoutSegments": [{
            "segmentOrder": 1,
            "sportType": {"sportTypeId": 1, "sportTypeKey": "running"},
            "workoutSteps": [{
                "type": "ExecutableStepDTO", "stepOrder": 1,
                "stepType": {"stepTypeId": 3, "stepTypeKey": "interval"},
                "endCondition": {"conditionTypeId": 3, "conditionTypeKey": "distance"},
                "endConditionValue": 1000.0,
                "targetType": {"workoutTargetTypeId": 1, "workoutTargetTypeKey": "no.target"},
            }],
        }],
    }
    wid = sched = None
    try:
        created = api.connectapi("/workout-service/workout", method="POST", json=payload)
        wid = created.get("workoutId")
        record("workout create", "PASS", f"id {wid}")
    except Exception as e:
        record("workout create", "FAIL", f"{type(e).__name__}: {e}")
        return
    try:
        tomorrow = (day + dt.timedelta(days=1)).isoformat()
        r = api.connectapi(f"/workout-service/schedule/{wid}",
                           method="POST", json={"date": tomorrow})
        sched = (r or {}).get("workoutScheduleId")
        record("workout schedule", "PASS", f"schedule {sched} on {tomorrow}")
    except Exception as e:
        record("workout schedule", "FAIL", f"{type(e).__name__}: {e}")
    try:
        if sched:
            api.connectapi(f"/workout-service/schedule/{sched}", method="DELETE")
        api.connectapi(f"/workout-service/workout/{wid}", method="DELETE")
        record("workout cleanup (deletes)", "PASS")
    except Exception as e:
        record("workout cleanup (deletes)", "FAIL",
               f"{type(e).__name__}: {e} — DELETE the recon workout manually in Connect!")


def summary():
    print("\n== summary (paste into docs/backlog/OPS-01-garmin-auth-plan-b.md) ==")
    print(f"Run: {dt.datetime.now():%Y-%m-%d %H:%M} · python {sys.version.split()[0]}")
    width = max((len(n) for n, _, _ in RESULTS), default=10)
    for name, status, note in RESULTS:
        print(f"| {name:<{width}} | {status:<5} | {note} |")
    fails = sum(1 for _, s, _ in RESULTS if s == "FAIL")
    print(f"\n{len(RESULTS)} checks, {fails} FAIL")


if __name__ == "__main__":
    sys.exit(main())
