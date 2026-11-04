import hashlib
import re
import uuid

import httpx
import pytest
from helpers import paired

from agents_app_connector.api.admin import PREFIX, create_admin_app
from agents_app_connector.db import device_tokens, devices


def client(app, host="127.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 5000)),
        base_url="http://admin" + PREFIX,
    )


@pytest.fixture
async def admin(pool, settings):
    async with client(create_admin_app(pool, settings)) as c:
        yield c


async def test_admin_pair_returns_a_code_not_a_token(admin, conn):
    r = await admin.post("/devices", json={"name": "note-air"})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"device_id", "name", "plugin", "code", "expires_at", "expires_in"}
    assert re.fullmatch(r"[0-9]{6}", body["code"])
    assert body["plugin"] == "sketch" and body["expires_in"] == 600
    assert "token" not in r.text
    # the code is stored as a hash only, in no column
    row = await (await conn.execute("SELECT code_sha256 FROM pairing_codes")).fetchone()
    assert row[0] == hashlib.sha256(body["code"].encode()).hexdigest()
    assert (await (await conn.execute("SELECT count(*) FROM device_tokens")).fetchone())[0] == 0


async def test_admin_list_states_and_revoke(admin, conn):
    created = (await admin.post("/devices", json={"name": "note-air"})).json()
    listed = await admin.get("/devices")
    assert listed.status_code == 200
    text = listed.text
    assert created["code"] not in text and "sha256" not in text and '"token"' not in text
    item = listed.json()[0]
    assert item["device_id"] == created["device_id"]
    assert item["state"] == "pending" and item["code_status"] == "pending"
    assert item["code_expires_at"] and item["paired_at"] is None

    await device_tokens.issue(conn, uuid.UUID(created["device_id"]))
    assert (await admin.get("/devices")).json()[0]["state"] == "active"

    assert (await admin.delete(f"/devices/{created['device_id']}")).status_code == 204
    assert (await admin.get("/devices")).json()[0]["state"] == "revoked"
    again = await admin.delete(f"/devices/{created['device_id']}")
    assert again.status_code == 404 and again.json()["error"] == "not_found"


async def test_admin_new_code_supersedes_for_a_pending_device(admin, conn):
    created = (await admin.post("/devices", json={"name": "a"})).json()
    r = await admin.post(f"/devices/{created['device_id']}/code")
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"code", "expires_at", "expires_in"} and body["expires_in"] == 600
    count = await (await conn.execute("SELECT count(*) FROM pairing_codes")).fetchone()
    assert count[0] == 2


async def test_admin_new_code_refused_for_paired_revoked_or_unknown(admin, conn):
    created = (await admin.post("/devices", json={"name": "a"})).json()
    did = uuid.UUID(created["device_id"])
    await device_tokens.issue(conn, did)
    r = await admin.post(f"/devices/{did}/code")
    assert r.status_code == 409 and r.json()["error"] == "already_paired"
    await admin.delete(f"/devices/{did}")
    assert (await admin.post(f"/devices/{did}/code")).status_code == 404
    unknown = await admin.post(f"/devices/{uuid.uuid4()}/code")
    assert unknown.status_code == 404 and unknown.json()["error"] == "not_found"


async def test_admin_too_many_pending_is_429_and_creates_nothing(admin, conn):
    for i in range(3):
        assert (await admin.post("/devices", json={"name": f"d{i}"})).status_code == 201
    r = await admin.post("/devices", json={"name": "d3"})
    assert r.status_code == 429 and r.json()["error"] == "too_many_pending"
    assert (await (await conn.execute("SELECT count(*) FROM devices")).fetchone())[0] == 3
    # a device that already waits can still get a fresh code
    first = (await admin.get("/devices")).json()[0]["device_id"]
    assert (await admin.post(f"/devices/{first}/code")).status_code == 201


async def test_delete_unknown_and_bad_id(admin):
    r = await admin.delete("/devices/00000000-0000-0000-0000-000000000000")
    assert r.status_code == 404
    assert (await admin.delete("/devices/nope")).status_code in (404, 422)


async def test_admin_rejects_outside_cidr(pool, settings):
    async with client(create_admin_app(pool, settings), host="10.0.0.5") as c:
        r = await c.get("/devices")
        assert r.status_code == 403 and r.json() == {"error": "forbidden"}
        assert (await c.get("/devices", headers={"Authorization": "Bearer x"})).status_code == 403


async def test_admin_rejects_device_token(pool, settings, conn):
    _, token = await paired(conn, "sketch", "a")
    auth = {"Authorization": f"Bearer {token}"}
    async with client(create_admin_app(pool, settings), host="10.0.0.5") as c:
        assert (await c.get("/devices", headers=auth)).status_code == 403
        assert (await c.post("/devices", json={"name": "x"}, headers=auth)).status_code == 403
    assert len(await devices.list_all(conn)) == 1


async def test_probes_exempt_from_cidr(pool, settings):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_admin_app(pool, settings), client=("10.0.0.5", 1)),
        base_url="http://admin",
    ) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200


async def test_admin_rejects_unknown_plugin(admin):
    r = await admin.post("/devices", json={"name": "a", "plugin": "nope"})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 81}])
async def test_admin_rejects_bad_name(admin, body):
    r = await admin.post("/devices", json=body)
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
