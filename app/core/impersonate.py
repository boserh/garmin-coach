"""Admin impersonation — "подивитися очима користувача", read-only.

Support questions ("чому в мене порожній дашборд?", "де мій план?") are answered by
looking at the same page the user sees. Reading it out of ``/ui`` row by row doesn't
show what the page actually renders, and asking for someone's password is not an
option — so an admin can borrow a session instead.

The session then carries BOTH ids: ``user_id`` is the impersonated account (so every
existing ``current_user`` route is user-scoped exactly as it already was, with no
per-router changes) and ``IMPERSONATOR_KEY`` is the admin who started it. That second
key is the whole feature: it drives the banner, it drives ``POST /impersonate/stop``,
and it's what the guards below key on.

Three hard limits, because a borrowed session is otherwise indistinguishable from the
real user acting:

* **Read-only.** ``current_user`` refuses any non-GET request (see
  :class:`ImpersonationReadOnly`) — checked at the dependency every authenticated route
  already depends on, so a new router can't forget it. An admin looking at an account
  must not be able to change its settings, delete its data or answer its check-ins;
  anything that ever showed up in the audit trail as the user would be a lie.
* **No money, no Garmin.** Same shape as the demo account's kill switch
  (``app.core.demo``): ``current_user`` sets :data:`IMPERSONATING` for the request and
  the two choke points every outbound call funnels through —
  ``app.garmin.runtime.user_runtime`` and ``app.analysis.client._get_client`` — refuse.
  Support must never spend the user's Claude budget or trip their Garmin rate limit.
* **No admin.** ``require_admin`` refuses while impersonating, and an admin account
  can't be impersonated in the first place (``app.routers.settings``) — so this can
  never be a route to admin rights held as somebody else.
"""
from contextvars import ContextVar

# Session key holding the real admin's id while a borrowed session is active. Its
# presence IS the "impersonating" state — there is no second flag to keep in sync.
IMPERSONATOR_KEY = "impersonator_id"
# Emails, cached in the session purely so the banner needs no DB lookup on every page.
IMPERSONATOR_EMAIL_KEY = "impersonator_email"
IMPERSONATED_EMAIL_KEY = "impersonated_email"

SESSION_KEYS = (IMPERSONATOR_KEY, IMPERSONATOR_EMAIL_KEY, IMPERSONATED_EMAIL_KEY)

IMPERSONATING: ContextVar[bool] = ContextVar("IMPERSONATING", default=False)

IMPERSONATE_DISABLED_MSG = (
    "👁 Режим перегляду адміністратором — запити до Garmin і Claude тут вимкнені."
)
IMPERSONATE_READONLY_MSG = (
    "👁 Режим перегляду адміністратором — тільки читання, зміни заборонені."
)

# Everything else is a state change and is refused while impersonating.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class ImpersonationReadOnly(Exception):
    """Raised by ``current_user`` on a write request made from a borrowed session."""


class ImpersonationUnavailable(Exception):
    """Raised instead of touching Garmin from a borrowed session (the last-resort net,
    mirroring ``DemoModeUnavailable``)."""


def clear(session: dict) -> None:
    """Drop every impersonation key from a session dict (used when stopping, and on
    every fresh login so a borrowed session can't outlive the sign-in that replaced it)."""
    for key in SESSION_KEYS:
        session.pop(key, None)
