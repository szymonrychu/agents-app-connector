import httpx
import pytest

from agents_app_connector.api.admin import PREFIX, create_admin_app
from agents_app_connector.db import devices


def client(app, host="127.0.0.1"):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 5000)),
        base_url="http://admin" + PREFIX,
    )


@pytest.fixture
async def admin(pool, settings):
    async with client(create_admin_app(pool, settings)) as c:
        yield c


async def test_admin_pair_list_revoke(admin, conn):
    r = await admin.post("/devices", json={"name": "note-air"})
    assert r.status_code == 201
    body = r.json()
    assert set(body) == {"device_id", "name", "plugin", "token"}
    assert body["plugin"] == "sketch" and body["token"]
    assert (await devices.by_token(conn, body["token"])).name == "note-air"

    listed = await admin.get("/devices")
    assert listed.status_code == 200
    text = listed.text
    assert body["token"] not in text and "token" not in text and "sha256" not in text
    assert listed.json()[0]["device_id"] == body["device_id"]

    assert (await admin.delete(f"/devices/{body['device_id']}")).status_code == 204
    assert await devices.by_token(conn, body["token"]) is None
    again = await admin.delete(f"/devices/{body['device_id']}")
    assert again.status_code == 404 and again.json()["error"] == "not_found"


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
    _, token = await devices.create(conn, "sketch", "a")
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
