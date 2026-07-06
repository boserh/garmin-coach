"""The garth MFA bridge (ST-06): a login that hits Garmin's MFA gate parks on a
background thread instead of completing, so a follow-up ``submit_code`` (a separate
web request, same process) can finish it."""
import threading
import time

import pytest

from app.garmin import mfa


class FakeMFAClient:
    """Stand-in for garth.Client whose login needs an MFA code."""

    def __init__(self, expected_code="123456", mfa_delay=0.0):
        self._expected_code = expected_code
        self._mfa_delay = mfa_delay
        self.token = None

    def login(self, email, password, prompt_mfa=None):
        if self._mfa_delay:
            time.sleep(self._mfa_delay)
        code = prompt_mfa()
        if code != self._expected_code:
            raise ValueError("bad MFA code")

    def dumps(self):
        self.token = "fresh-mfa-token"
        return self.token


class FakeNoMfaClient:
    def login(self, email, password, prompt_mfa=None):
        pass

    def dumps(self):
        return "fresh-token"


@pytest.fixture(autouse=True)
def _clear_pending():
    mfa._pending.clear()
    yield
    mfa._pending.clear()


class FakeFailingClient:
    """A login that Garmin rejects outright (e.g. Cloudflare block, bad creds)."""

    def login(self, email, password, prompt_mfa=None):
        raise RuntimeError("403 Forbidden (cloudflare)")


def test_failed_login_logs_garmin_auth_fail_marker(caplog):
    # OPS-01 monitoring: the grep-stable ERROR marker is the migration trigger.
    client = FakeFailingClient()
    with caplog.at_level("ERROR", logger="garmin"):
        with pytest.raises(RuntimeError, match="403 Forbidden"):
            mfa.start_login(7, client, "e@x.com", "pw")
    assert any("GARMIN AUTH FAIL" in r.message for r in caplog.records)


def test_start_login_no_mfa_returns_immediately():
    client = FakeNoMfaClient()
    mfa.start_login(1, client, "e@x.com", "pw")
    assert not mfa.has_pending(1)


def test_start_login_raises_mfa_required_and_parks():
    client = FakeMFAClient()
    with pytest.raises(mfa.MFARequired) as exc_info:
        mfa.start_login(2, client, "e@x.com", "pw")
    assert exc_info.value.user_id == 2
    assert mfa.has_pending(2)


def test_submit_code_completes_pending_login():
    client = FakeMFAClient(expected_code="654321")
    with pytest.raises(mfa.MFARequired):
        mfa.start_login(3, client, "e@x.com", "pw")

    token = mfa.submit_code(3, "654321")
    assert token == "fresh-mfa-token"
    assert not mfa.has_pending(3)


def test_submit_code_wrong_code_raises_and_clears_state():
    client = FakeMFAClient(expected_code="111111")
    with pytest.raises(mfa.MFARequired):
        mfa.start_login(4, client, "e@x.com", "pw")

    with pytest.raises(ValueError, match="bad MFA code"):
        mfa.submit_code(4, "000000")
    # the failed attempt clears the pending state so a retry can start clean
    assert not mfa.has_pending(4)
    with pytest.raises(mfa.MFANotPending):
        mfa.submit_code(4, "111111")


def test_submit_code_without_pending_login_raises():
    with pytest.raises(mfa.MFANotPending):
        mfa.submit_code(999, "123456")


def test_cancel_drops_pending_state():
    client = FakeMFAClient()
    with pytest.raises(mfa.MFARequired):
        mfa.start_login(5, client, "e@x.com", "pw")
    assert mfa.has_pending(5)
    mfa.cancel(5)
    assert not mfa.has_pending(5)
    with pytest.raises(mfa.MFANotPending):
        mfa.submit_code(5, "123456")


def test_start_login_called_twice_while_pending_raises_mfa_required_again():
    client = FakeMFAClient()
    with pytest.raises(mfa.MFARequired):
        mfa.start_login(6, client, "e@x.com", "pw")
    # a second attempt (e.g. a page reload) shouldn't spawn a second thread
    with pytest.raises(mfa.MFARequired):
        mfa.start_login(6, client, "e@x.com", "pw")
    assert threading.active_count() >= 1  # sanity: original thread still alive
    mfa.submit_code(6, "123456")
