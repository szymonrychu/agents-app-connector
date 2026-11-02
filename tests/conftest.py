import httpx
import psycopg
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from helpers import PREFIX, Sketch, bearer, files, meta, png, post

from agents_app_connector.api.device import create_device_app
from agents_app_connector.db import devices
from agents_app_connector.settings import Settings
from agents_app_connector.storage import AttachmentStore

pytest_plugins = ["agents_common.testing"]

PACKAGE = "agents_app_connector"
PNG = png()


@pytest.fixture
def url(migrated_db):
    return migrated_db(PACKAGE)


@pytest.fixture
async def conn(url):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as c:
        yield c


@pytest.fixture
async def pool(url):
    p = await open_pool(url)
    yield p
    await p.close()


@pytest.fixture
def fake():
    return FakeOrchestrator()


@pytest.fixture
async def orchestrator(fake):
    transport = httpx.ASGITransport(app=fake.app)
    async with OrchestratorClient("http://orch", fake.token, transport=transport) as c:
        yield c


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="postgresql://u:p@db/x",
        orchestrator_url="http://orch",
        orchestrator_token="tok",
        data_dir=str(tmp_path),
    )


@pytest.fixture
def store(tmp_path):
    return AttachmentStore(tmp_path / "store")


@pytest.fixture
def make_app(pool, settings, store):
    def make(**overrides):
        return create_device_app(pool, settings.model_copy(update=overrides), [Sketch()], store)

    return make


@pytest.fixture
async def device_api(make_app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=make_app()), base_url="http://dev" + PREFIX
    ) as c:
        yield c


@pytest.fixture
async def token(conn):
    return (await devices.create(conn, "sketch", "boox"))[1]


@pytest.fixture
def upload(device_api, token):
    async def go(conv="current", png_bytes=None, caption=None):
        png_bytes = png_bytes or PNG
        extra = {} if caption is None else {"caption": caption}
        resp = await post(
            device_api, token, meta(conversation=conv, **extra), files(png_bytes, with_strokes=True)
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        return body["sketch_id"], body["conversation_id"]

    return go


@pytest.fixture
def say(device_api, token):
    async def go(conv, text):
        import uuid

        resp = await device_api.post(
            f"/conversations/{conv}/messages",
            json={"client_id": str(uuid.uuid4()), "text": text},
            headers=bearer(token),
        )
        assert resp.status_code == 202, resp.text
        return resp.json()["message_id"]

    return go


@pytest.fixture
def preset(fake):
    return fake.add_preset("sketch-translator")
