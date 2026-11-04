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


def _request(peer, headers):
    from starlette.requests import Request

    scope = {
        "type": "http",
        "client": (peer, 1) if peer else None,
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
    }
    return Request(scope)


def test_client_ip_trusts_forwarding_headers_only_from_trusted_peers():
    import ipaddress

    from agents_app_connector.api.limits import client_ip

    trusted = [ipaddress.ip_network("10.0.0.0/8")]
    assert client_ip(_request("10.1.2.3", {"X-Real-IP": "203.0.113.1"}), trusted) == "203.0.113.1"
    xff = {"X-Forwarded-For": "6.6.6.6, 203.0.113.2"}
    assert client_ip(_request("10.1.2.3", xff), trusted) == "203.0.113.2"
    assert client_ip(_request("10.1.2.3", {"X-Real-IP": "junk"}), trusted) == "10.1.2.3"
    assert client_ip(_request("10.1.2.3", {}), trusted) == "10.1.2.3"
    assert client_ip(_request("192.0.2.1", {"X-Real-IP": "203.0.113.1"}), trusted) == "192.0.2.1"
    assert client_ip(_request("10.1.2.3", {"X-Real-IP": "203.0.113.1"}), []) == "10.1.2.3"
    assert client_ip(_request(None, {}), trusted) == "unknown"
