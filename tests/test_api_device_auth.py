import httpx
import pytest
from fastapi import Request

from agents_app_connector.api.device import create_device_app
from agents_app_connector.db import devices
from agents_app_connector.plugins.sketch.router import build_router

PREFIX = "/app/v1/sketch"
CONFIG = {
    "max_long_edge_px": 1568,
    "max_png_bytes": 6291456,
    "max_strokes_bytes": 2097152,
    "max_pages": 4,
    "max_upload_bytes": 8388608,
    "min_edge_px": 200,
    "api_version": 1,
}


class SketchStub:
    id = "sketch"
    prefix = PREFIX

    def router(self, deps):
        router = build_router(deps)

        @router.post("/echo")
        async def echo(request: Request):
            return {"size": len(await request.body())}

        return router


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_client(app, base=PREFIX):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://dev" + base)


@pytest.fixture
def make_app(pool, settings):
    def make(**overrides):
        return create_device_app(
            pool, settings.model_copy(update=overrides), [SketchStub()], store=None
        )

    return make


@pytest.fixture
async def device_api(make_app):
    async with make_client(make_app()) as c:
        yield c


@pytest.fixture
async def token(conn):
    return (await devices.create(conn, "sketch", "boox"))[1]


async def test_config_requires_token(device_api):
    r = await device_api.get("/config")
    assert r.status_code == 401 and r.json()["error"] == "unauthorized"


async def test_config_returns_settings(device_api, token):
    r = await device_api.get("/config", headers=bearer(token))
    assert r.status_code == 200 and r.json() == CONFIG


@pytest.mark.parametrize(
    "header", ["Basic abc", "Bearer", "Bearer ", "Bearer a b", "abc", "Bearer unknown-token"]
)
async def test_bad_tokens_one_body(device_api, token, header):
    ref = await device_api.get("/config")
    r = await device_api.get("/config", headers={"Authorization": header})
    assert r.status_code == 401 and r.json() == ref.json()


async def test_revoked_token_401_at_once(device_api, token, conn):
    assert (await device_api.get("/config", headers=bearer(token))).status_code == 200
    row = await devices.by_token(conn, token)
    assert await devices.revoke(conn, row.id)
    assert (await device_api.get("/config", headers=bearer(token))).status_code == 401


async def test_other_plugin_token_401(device_api, conn):
    _, other = await devices.create(conn, "other", "x")
    assert (await device_api.get("/config", headers=bearer(other))).status_code == 401


async def test_rate_limit_429(make_app, token):
    async with make_client(make_app(rate_per_minute=2)) as c:
        assert (await c.get("/config", headers=bearer(token))).status_code == 200
        assert (await c.get("/config", headers=bearer(token))).status_code == 200
        r = await c.get("/config", headers=bearer(token))
        assert r.status_code == 429 and r.json()["error"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1
        # unauthenticated calls do not spend the device bucket
        assert (await c.get("/config")).status_code == 401


async def test_rate_limit_is_per_device(make_app, token, conn):
    _, other = await devices.create(conn, "sketch", "second")
    async with make_client(make_app(rate_per_minute=1)) as c:
        assert (await c.get("/config", headers=bearer(token))).status_code == 200
        assert (await c.get("/config", headers=bearer(token))).status_code == 429
        assert (await c.get("/config", headers=bearer(other))).status_code == 200


async def test_body_over_cap_413_with_and_without_content_length(make_app, token):
    app = make_app()
    async with make_client(app) as c:
        limit = 6291456 + 2097152 + 16384 + 65536
        ok = await c.post("/echo", content=b"x" * 1000, headers=bearer(token))
        assert ok.status_code == 200 and ok.json() == {"size": 1000}
        big = b"x" * (limit + 1)
        r = await c.post("/echo", content=big, headers=bearer(token))
        assert r.status_code == 413 and r.json()["error"] == "too_large"

        async def chunks():
            yield big[: limit // 2]
            yield big[limit // 2 :]

        r = await c.post("/echo", content=chunks(), headers=bearer(token))
        assert "content-length" not in r.request.headers
        assert r.status_code == 413 and r.json()["error"] == "too_large"


async def test_probes_open(make_app):
    async with make_client(make_app(), base="") as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200


async def test_admin_paths_404_on_device_app(make_app, token):
    async with make_client(make_app(), base="") as c:
        for path in ("/app/admin/v1/devices", "/devices"):
            assert (await c.get(path, headers=bearer(token))).status_code == 404
            assert (await c.post(path, json={"name": "x"}, headers=bearer(token))).status_code in (
                404,
                405,
            )
