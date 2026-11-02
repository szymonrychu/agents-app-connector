"""The `app` process: device API, dispatch worker, reply tracker and retention in one process.

One replica only: the rate limiter is in-process and the loops run under a Postgres advisory
lock, so a second instance waits for the first to stop instead of working in parallel.
"""

import asyncio
import contextlib
import logging

from agents_common.db.pool import open_pool
from agents_common.orchestrator import OrchestratorClient
from agents_common.runtime import advisory_singleton

from agents_app_connector.api.device import create_device_app
from agents_app_connector.plugins import REGISTRY
from agents_app_connector.retention import Retention
from agents_app_connector.serve import serve_asgi
from agents_app_connector.settings import Settings
from agents_app_connector.storage import AttachmentStore
from agents_app_connector.tracker import Tracker
from agents_app_connector.worker import Worker

log = logging.getLogger(__name__)

SINGLETON_KEY = "agents-app-connector-app"


async def _loop(name: str, unit, interval: float, stop: asyncio.Event) -> None:
    """Tick every `interval` seconds until stopped; a failing tick is logged, never fatal."""
    while not stop.is_set():
        try:
            await unit.tick()
        except Exception:
            log.exception("%s tick failed", name)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), interval)


async def run_app(
    settings: Settings,
    *,
    orchestrator: OrchestratorClient | None = None,
    stop: asyncio.Event,
) -> None:
    """Serve until `stop` is set. An injected `orchestrator` is not closed here."""
    pool = await open_pool(settings.database_url.get_secret_value())
    owned = orchestrator is None
    if orchestrator is None:
        orchestrator = OrchestratorClient(
            settings.orchestrator_url, settings.orchestrator_token.get_secret_value()
        )
    try:
        store = AttachmentStore(settings.data_dir)
        plugins = list(REGISTRY.values())
        app = create_device_app(pool, settings, plugins, store, orchestrator=orchestrator)
        units = {
            "worker": Worker(orchestrator, pool, settings, store),
            "tracker": Tracker(orchestrator, pool, settings),
            "retention": Retention(pool, store, retention_days=settings.retention_days),
        }
        async with advisory_singleton(pool, SINGLETON_KEY):
            tasks = [
                asyncio.create_task(_loop(name, unit, settings.poll_seconds, stop), name=name)
                for name, unit in units.items()
            ]
            server = asyncio.create_task(
                serve_asgi(
                    app,
                    settings.http_host,
                    settings.device_port,
                    settings.shutdown_grace_seconds,
                    stop,
                ),
                name="device-api",
            )
            try:
                await asyncio.wait({server}, return_when=asyncio.FIRST_COMPLETED)
                stop.set()
                await server
            finally:
                stop.set()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        if owned:
            await orchestrator.aclose()
        await pool.close()
