"""Device routes of the sketch plugin."""

from fastapi import APIRouter, Depends

from agents_app_connector.api.auth import device_auth
from agents_app_connector.plugins import PluginDeps

PLUGIN_ID = "sketch"
API_VERSION = 1


def build_router(deps: PluginDeps) -> APIRouter:
    router = APIRouter()

    @router.get("/config", dependencies=[Depends(device_auth(PLUGIN_ID))])
    async def config() -> dict[str, int]:
        s = deps.settings
        return {
            "max_long_edge_px": s.max_long_edge_px,
            "max_png_bytes": s.max_png_bytes,
            "max_strokes_bytes": s.max_strokes_bytes,
            "api_version": API_VERSION,
        }

    return router
