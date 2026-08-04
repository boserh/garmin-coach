"""The demo account's kill switch.

``User.is_demo`` marks the single, shared, read-only walkthrough account (seeded fake
data — see ``app.demo``). It must never trigger a real Garmin or Anthropic call, so
this isn't left to every router remembering to check ``user.is_demo`` itself:
``current_user`` (``app.core.auth``) sets ``IS_DEMO`` for the lifetime of the request,
and the two hard choke points every such call funnels through —
``app.garmin.runtime.user_runtime`` and ``app.analysis.client._get_client`` — refuse
outright when it's set. Background jobs never see a request at all, so they're covered
separately: ``app.db.users.eligible_users`` excludes ``is_demo`` accounts entirely.
"""
from contextvars import ContextVar

IS_DEMO: ContextVar[bool] = ContextVar("IS_DEMO", default=False)

DEMO_DISABLED_MSG = (
    "🎭 Це демо-акаунт із фейковими даними — запити до Garmin і Claude тут вимкнені."
)
