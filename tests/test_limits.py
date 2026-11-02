from agents_app_connector.api.limits import RateLimiter


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_bucket_refills():
    clock = Clock()
    rl = RateLimiter(2, now=clock)
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")
    assert rl.retry_after("a") == 30
    clock.t += 60
    assert rl.allow("a") and rl.allow("a")
    assert not rl.allow("a")


def test_keys_are_independent_and_capacity_capped():
    clock = Clock()
    rl = RateLimiter(1, now=clock)
    assert rl.allow("a")
    assert rl.allow("b")
    assert not rl.allow("a")
    clock.t += 600
    assert rl.allow("a")
    assert not rl.allow("a")
