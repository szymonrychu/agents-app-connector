"""Serve an ASGI app with uvicorn until a stop event is set."""

import asyncio

import uvicorn


async def serve_asgi(app, host: str, port: int, grace_seconds: float, stop: asyncio.Event) -> None:
    """Proxy headers stay off: the socket peer is what the CIDR allowlist checks."""
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        proxy_headers=False,
        log_config=None,
        timeout_graceful_shutdown=int(grace_seconds),
    )
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    async def watch() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(watch())
    try:
        await server.serve()
    finally:
        watcher.cancel()
