"""SEC-01: the in-memory sliding-window rate limiter."""
from app.core.ratelimit import RateLimiter


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_allows_up_to_limit_then_blocks():
    clock = _Clock()
    rl = RateLimiter(3, 60, now=clock)
    assert [rl.allow("a") for _ in range(3)] == [True, True, True]
    assert rl.allow("a") is False  # 4th within window
    assert rl.allow("a") is False


def test_keys_are_independent():
    rl = RateLimiter(1, 60, now=_Clock())
    assert rl.allow("a") is True
    assert rl.allow("b") is True  # different key, own budget
    assert rl.allow("a") is False


def test_window_slides():
    clock = _Clock()
    rl = RateLimiter(2, 60, now=clock)
    assert rl.allow("a") and rl.allow("a")
    assert rl.allow("a") is False
    clock.t = 61  # both hits now older than the window
    assert rl.allow("a") is True


def test_blocked_attempt_does_not_extend_window():
    clock = _Clock()
    rl = RateLimiter(1, 60, now=clock)
    assert rl.allow("a") is True          # hit at t=0
    clock.t = 30
    assert rl.allow("a") is False         # blocked, must NOT record t=30
    clock.t = 61                          # 61 > 0+60 → original hit expired
    assert rl.allow("a") is True


def test_zero_limit_disables():
    rl = RateLimiter(0, 60)
    assert all(rl.allow("a") for _ in range(100))


def test_reset():
    rl = RateLimiter(1, 60)
    assert rl.allow("a") is True
    assert rl.allow("a") is False
    rl.reset("a")
    assert rl.allow("a") is True
    rl.reset()  # clear everything
    assert rl.allow("a") is True
