"""User-scoped persistence, split by domain into a package (CODE-AUDIT-2026-07 B1).

The old flat ``app/garmin/repository.py`` (1600+ lines) is now a package of domain modules
(``core``/``stats``/``state``/``plans``); this facade re-exports every public name so
``from app.garmin import repository`` and every ``repository.X`` call site — and the tests'
monkeypatch paths — keep working with zero behaviour change (the CODE-01 recipe)."""
from app.garmin.repository.core import *  # noqa: F401,F403
from app.garmin.repository.plans import *  # noqa: F401,F403

# _sanitize_strength is used externally (routers/plan.py, analysis/plans.py, tests), so it
# must be re-exported explicitly — ``import *`` skips underscore-prefixed names.
from app.garmin.repository.plans import _sanitize_strength  # noqa: F401
from app.garmin.repository.state import *  # noqa: F401,F403
from app.garmin.repository.stats import *  # noqa: F401,F403
