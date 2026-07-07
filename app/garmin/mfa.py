"""Bridge between garth's synchronous MFA prompt and the web ``/settings`` route.

The installed garth (0.4.47) has no ``return_on_mfa``/``resume_login`` pair — ``login()``
just calls a ``prompt_mfa`` callback and blocks until it returns a code. To let a web
request kick off a login, discover Garmin wants MFA, and only *later* (a follow-up
request with the code) finish it, we run ``login()`` on a background thread whose
``prompt_mfa`` parks on a queue: the initiating call waits briefly for either a fast
(no-MFA) result or the MFA gate, then returns control to the caller either way.

Deliberately per-process, in-memory (a module-level dict, TTL ~10 min) — an MFA
trigger from the bot (a different process than the web) can't be completed there;
the bot just points the user at the web `/settings` page, which starts its own fresh
login attempt in its own process. See ST-06 in the backlog.
"""
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("garmin")

MFA_CODE_TIMEOUT_S = 600  # how long a paused login waits for the code (~10 min)
_START_WAIT_S = 25  # how long start_login blocks for a fast result or the MFA gate
_SUBMIT_WAIT_S = 20  # how long submit_code blocks for the login to finish


class MFARequired(Exception):
    """Raised by a login attempt when Garmin is asking for an MFA code. The caller
    should tell the user to finish the login at /settings."""

    def __init__(self, user_id: int):
        self.user_id = user_id
        super().__init__(f"Garmin MFA required for user {user_id}")


class MFANotPending(Exception):
    """A code was submitted but nothing is waiting for one — expired, never
    started, or already consumed. The user needs to restart the login attempt."""


@dataclass
class _LoginState:
    mfa_requested: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    code_queue: "queue.Queue[str]" = field(default_factory=lambda: queue.Queue(maxsize=1))
    ok: bool = False
    token: Optional[str] = None
    error: Optional[BaseException] = None


_lock = threading.Lock()
_pending: Dict[int, _LoginState] = {}


def has_pending(user_id: int) -> bool:
    """True if this user has a login parked on the MFA gate, waiting for a code."""
    state = _pending.get(user_id)
    return state is not None and state.mfa_requested.is_set() and not state.done.is_set()


def start_login(user_id: int, client, email: str, password: str) -> None:
    """Run ``client.login(email, password)`` on a background thread. Blocks the
    calling thread briefly for either a fast (no-MFA) result or Garmin's MFA gate.

    On a fast success, returns normally (``client`` is logged in). On MFA, raises
    ``MFARequired`` and leaves the thread parked on the code queue for a follow-up
    ``submit_code``. On a fast failure, re-raises the underlying error.
    """
    with _lock:
        existing = _pending.get(user_id)
        if existing is not None and not existing.done.is_set():
            if existing.mfa_requested.is_set():
                raise MFARequired(user_id)
            state = existing  # a start_login is already in flight — just wait on it
        else:
            state = _LoginState()
            _pending[user_id] = state

            def _run():
                def prompt_mfa():
                    state.mfa_requested.set()
                    try:
                        return state.code_queue.get(timeout=MFA_CODE_TIMEOUT_S)
                    except queue.Empty:
                        raise TimeoutError("MFA code not provided in time")

                try:
                    client.login(email, password, prompt_mfa=prompt_mfa)
                    state.ok, state.token = True, client.dumps()
                except Exception as exc:  # surfaced to whoever is waiting
                    state.error = exc
                    # OPS-01 monitoring: a fresh-login failure is the trigger for
                    # the garth → python-garminconnect migration. Keep the marker
                    # grep-stable: `grep "GARMIN AUTH FAIL" bot.log`.
                    logger.error(
                        "GARMIN AUTH FAIL: fresh login failed for user %s: %r",
                        user_id, exc,
                    )
                finally:
                    state.done.set()

            threading.Thread(
                target=_run, daemon=True, name=f"garmin-login-{user_id}"
            ).start()

    deadline = time.monotonic() + _START_WAIT_S
    while time.monotonic() < deadline and not (state.done.is_set() or state.mfa_requested.is_set()):
        time.sleep(0.1)

    if state.done.is_set():
        _pending.pop(user_id, None)
        if state.ok:
            return
        raise state.error
    if state.mfa_requested.is_set():
        raise MFARequired(user_id)
    raise TimeoutError("Garmin login timed out")


def submit_code(user_id: int, code: str) -> str:
    """Deliver the user's MFA code to the paused login and wait for it to finish.
    Returns the fresh garth session token on success."""
    state = _pending.get(user_id)
    if state is None or state.done.is_set() or not state.mfa_requested.is_set():
        raise MFANotPending(f"No pending MFA login for user {user_id}")
    state.code_queue.put(code)
    if not state.done.wait(timeout=_SUBMIT_WAIT_S):
        raise MFANotPending("Still waiting on Garmin — try again in a moment")
    _pending.pop(user_id, None)
    if not state.ok:
        raise state.error or RuntimeError("Garmin MFA login failed")
    return state.token


def cancel(user_id: int) -> None:
    """Drop any pending state so the next attempt starts a clean login."""
    _pending.pop(user_id, None)
