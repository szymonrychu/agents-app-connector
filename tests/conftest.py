import httpx
import psycopg
import pytest
from agents_common.db.pool import open_pool
from agents_common.fake_orchestrator import FakeOrchestrator
from agents_common.orchestrator import OrchestratorClient
from helpers import PREFIX, Sketch

from agents_app_connector.api.device import create_device_app
from agents_app_connector.db import devices
from agents_app_connector.settings import Settings
from agents_app_connector.storage import AttachmentStore

pytest_plugins = ["agents_common.testing"]

PACKAGE = "agents_app_connector"


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
