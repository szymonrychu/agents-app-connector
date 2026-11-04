import asyncio
import logging
import re

import httpx
import pytest
from helpers import PREFIX, bearer

from agents_app_connector.db import devices, pairing


async def code_for(conn, name="boox", ttl=600):
    dev = await devices.create(conn, "sketch", name)
    return dev, (await pairing.issue(conn, dev.id, ttl, 3)).code


def client(make_app, host="10.9.0.1", **overrides):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app(**overrides), client=(host, 4000)),
        base_url="http://dev" + PREFIX,
    )


def wrong_code(real: str) -> str:
    return "000000" if real != "000000" else "111111"


async def test_exchange_returns_a_working_token_with_no_store(make_app, conn):
    dev, code = await code_for(conn)
    async with client(make_app) as api:
        r = await api.post("/pair", json={"code": code})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"
        body = r.json()
        assert body["device_id"] == str(dev.id) and body["name"] == "boox"
        assert body["plugin"] == "sketch" and len(body["token"]) >= 43
        # no bearer header was needed, and the new token authenticates
        assert (await api.get("/config", headers=bearer(body["token"]))).status_code == 200
        again = await api.post("/pair", json={"code": code})
        assert again.status_code == 400 and again.json()["error"] == "invalid_code"
    row = await (await conn.execute("SELECT redeemed_ip FROM pairing_codes")).fetchone()
    assert row[0] == "10.9.0.1"


async def test_concurrent_exchanges_give_exactly_one_token(make_app, conn):
    _, code = await code_for(conn)
    async with client(make_app, pairing_ip_per_minute=50) as api:
        results = await asyncio.gather(*(api.post("/pair", json={"code": code}) for _ in range(6)))
    assert sorted(r.status_code for r in results) == [200, 400, 400, 400, 400, 400]


async def test_every_failure_cause_has_the_same_body(make_app, conn):
    expired_dev, expired = await code_for(conn, "e")
    await conn.execute(
        "UPDATE pairing_codes SET expires_at = now() - interval '1 second' WHERE device_id = %s",
        (expired_dev.id,),
    )
    revoked_dev, revoked = await code_for(conn, "r")
    await devices.revoke(conn, revoked_dev.id)
    old_dev, old = await code_for(conn, "o")
    await pairing.issue(conn, old_dev.id, 600, 3)  # supersedes `old`
    _, unknown = await code_for(conn, "u")
    unknown = wrong_code(unknown)
    bodies = set()
    async with client(make_app, pairing_ip_per_minute=50, pairing_global_per_minute=50) as api:
        for code in (expired, revoked, old, unknown):
            r = await api.post("/pair", json={"code": code})
            assert r.status_code == 400
            bodies.add(r.text)
    assert len(bodies) == 1


async def test_failures_lock_the_pending_code_for_good(make_app, conn):
    _, code = await code_for(conn)
    bad = wrong_code(code)
    async with client(make_app, pairing_ip_per_minute=50, pairing_global_per_minute=50) as api:
        for _ in range(5):
            assert (await api.post("/pair", json={"code": bad})).status_code == 400
        late = await api.post("/pair", json={"code": code})
    assert late.status_code == 400 and late.json()["error"] == "invalid_code"
    status = (await (await conn.execute("SELECT status FROM pairing_codes")).fetchone())[0]
    assert status == "locked"


@pytest.mark.parametrize(
    "body", [{"code": "12345"}, {"code": "1234567"}, {"code": "12345a"}, {"code": 123456}]
)
async def test_malformed_bodies_are_422_and_count_as_failures(make_app, conn, body):
    _, code = await code_for(conn)
    async with client(make_app) as api:
        r = await api.post("/pair", json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    n = (await (await conn.execute("SELECT failed_attempts FROM pairing_codes")).fetchone())[0]
    assert n == 1 and code


async def test_extra_fields_and_not_json_are_422(make_app):
    async with client(make_app) as api:
        extra = await api.post("/pair", json={"code": "123456", "x": 1})
        junk = await api.post("/pair", content=b"nope")
    assert extra.status_code == 422 and junk.status_code == 422


async def test_per_ip_limit_is_429_with_retry_after(make_app, conn):
    async with client(make_app, pairing_ip_per_minute=2) as api:
        for _ in range(2):
            assert (await api.post("/pair", json={"code": "000000"})).status_code == 400
        r = await api.post("/pair", json={"code": "000000"})
    assert r.status_code == 429 and r.json()["error"] == "rate_limited"
    assert int(r.headers["retry-after"]) >= 1


async def test_global_limit_applies_across_ips(make_app):
    app = make_app(pairing_global_per_minute=2, pairing_ip_per_minute=10)
    statuses = []
    for host in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        transport = httpx.ASGITransport(app=app, client=(host, 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://dev" + PREFIX) as api:
            statuses.append((await api.post("/pair", json={"code": "000000"})).status_code)
    assert statuses == [400, 400, 429]


async def test_x_real_ip_is_trusted_only_from_the_trusted_proxies(make_app, conn):
    headers = {"X-Real-IP": "203.0.113.5"}
    _, code = await code_for(conn)
    async with client(make_app, host="10.42.0.7", trusted_proxy_cidrs=["10.42.0.0/16"]) as api:
        assert (await api.post("/pair", json={"code": code}, headers=headers)).status_code == 200
    row = await (await conn.execute("SELECT redeemed_ip FROM pairing_codes")).fetchone()
    assert row[0] == "203.0.113.5"

    # an untrusted peer cannot choose its own bucket: the header is ignored
    async with client(make_app, host="198.51.100.9", pairing_ip_per_minute=1) as api:
        first = await api.post("/pair", json={"code": "000000"}, headers={"X-Real-IP": "1.1.1.1"})
        second = await api.post("/pair", json={"code": "000000"}, headers={"X-Real-IP": "2.2.2.2"})
    assert (first.status_code, second.status_code) == (400, 429)


async def test_revoked_device_token_issued_by_exchange_stops_working(make_app, conn):
    dev, code = await code_for(conn)
    async with client(make_app) as api:
        token = (await api.post("/pair", json={"code": code})).json()["token"]
        await devices.revoke(conn, dev.id)
        assert (await api.get("/config", headers=bearer(token))).status_code == 401


async def test_logs_never_contain_the_code_or_the_token(make_app, conn, caplog):
    _, code = await code_for(conn)
    with caplog.at_level(logging.DEBUG):
        async with client(make_app, pairing_ip_per_minute=50) as api:
            await api.post("/pair", json={"code": wrong_code(code)})
            token = (await api.post("/pair", json={"code": code})).json()["token"]
    assert code not in caplog.text and token not in caplog.text
    assert re.search(r"paired from", caplog.text)
