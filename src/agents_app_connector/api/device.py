"""Public device app: plugin routers behind bearer auth, rate limit and a body cap."""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from psycopg_pool import AsyncConnectionPool

from agents_app_connector.api.admin import add_probes
from agents_app_connector.api.errors import add_error_handlers
from agents_app_connector.api.events import EventHub
from agents_app_connector.api.limits import BodyCap, RateLimiter
from agents_app_connector.plugins import ClientPlugin, PluginDeps
from agents_app_connector.settings import Settings


def create_device_app(
    pool: AsyncConnectionPool,
    settings: Settings,
    plugins: Sequence[ClientPlugin],
    store,
    orchestrator=None,
    taskboard: httpx.AsyncClient | None = None,
) -> FastAPI:
    limiter = RateLimiter(settings.rate_per_minute)
    hub = EventHub(settings.database_url.get_secret_value(), pool, settings.poll_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await hub.start()
        try:
            yield
        finally:
            await hub.stop()

    app = FastAPI(
        lifespan=lifespan,
        title="agents-app-connector device API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.pool = pool
    app.state.limiter = limiter
    deps = PluginDeps(
        pool=pool,
        settings=settings,
        store=store,
        limiter=limiter,
        hub=hub,
        orchestrator=orchestrator,
        taskboard=taskboard,
    )
    for plugin in plugins:
        app.include_router(plugin.router(deps), prefix=plugin.prefix)
    app.add_middleware(BodyCap, max_bytes=settings.max_body_bytes)
    add_error_handlers(app)
    add_probes(app, pool)
    return app
