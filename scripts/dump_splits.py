"""Print the RAW ``/activity-service/activity/{id}/splits`` response Garmin returns for
one activity — no parsing, no caching, no Anthropic call.

Why: ``app.garmin.client.fetch_activity_splits`` reads only the ``lapDTOs`` key and
assumes (per its own docstring) that it holds "one row per structured-workout step
actually executed" — warmup/tempo/cooldown. A tempo run's step_match came back citing an
"actual" pace that matched neither the tempo lap NOR the warmup/cooldown laps Garmin
Connect's own Intervals tab showed for the same activity, which means ``lapDTOs`` is
very likely NOT what the Intervals tab renders (that's probably a differently-shaped
field in the same response, e.g. ``typedSplitSummaries`` or similar — Garmin's own
"Splits" (auto per-km) and "Intervals" (structured-workout steps) views are commonly two
different arrays in one JSON blob). This script exists to see the actual field names and
decide how ``fetch_activity_splits`` should really read them.

Costs: 0 Anthropic calls. Does one real (free, rate-limited-as-usual) Garmin API call.

Usage (venv interpreter, from the repo root)::

    ./venv/bin/python -m scripts.dump_splits --email me@example.com --activity-id 123456789
    ./venv/bin/python -m scripts.dump_splits --email me@example.com --activity-id 123456789 \\
        --out /tmp/splits.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _run(email: str, activity_id: int, out: str | None) -> int:
    from app.cli import _UserNotFound, cli_user
    from app.garmin.client import _api

    try:
        async with cli_user(email, garmin=True):
            data = _api(f"/activity-service/activity/{activity_id}/splits")
    except _UserNotFound:
        print(f"User {email} not found.", file=sys.stderr)
        return 1

    text = json.dumps(data, indent=2, ensure_ascii=False)
    if out:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"Wrote {len(text)} bytes to {out}")
        print("Top-level keys:", sorted(data.keys()) if isinstance(data, dict) else type(data))
    else:
        print(text)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--email", required=True)
    p.add_argument("--activity-id", required=True, type=int)
    p.add_argument("--out", default=None, help="write to this file instead of stdout")
    args = p.parse_args(argv)
    return asyncio.run(_run(args.email, args.activity_id, args.out))


if __name__ == "__main__":
    raise SystemExit(main())
