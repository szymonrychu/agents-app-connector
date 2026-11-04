"""Command line entrypoint: migrate, admin, app and purge subcommands."""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from agents_common.db.migrate import run_migrations
from agents_common.db.pool import open_pool
from agents_common.runtime import run_service
from agents_common.settings import SettingsError, load_settings
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from agents_app_connector import __version__
from agents_app_connector.api.admin import create_admin_app
from agents_app_connector.clock import utcnow
from agents_app_connector.process import run_app
from agents_app_connector.retention import PurgeResult, purge
from agents_app_connector.serve import serve_asgi
from agents_app_connector.settings import Settings
from agents_app_connector.storage import AttachmentStore

EXIT_BAD_SETTINGS = 78

SUBCOMMANDS = {
    "migrate": "apply database migrations",
    "admin": "run the admin API (device pairing, behind SSO and a CIDR allowlist)",
    "app": "run the device API, dispatch worker, reply tracker and retention",
    "purge": "delete expired data once and exit",
}

log = logging.getLogger("agents_app_connector")


class MigrateSettings(BaseSettings):
    """Only the database URL: migrate needs no orchestrator access."""

    model_config = SettingsConfigDict(env_prefix="APPCONN_", extra="ignore")

    database_url: SecretStr = Field(repr=False)
    log_level: str = "INFO"


def _load_migrate() -> MigrateSettings:
    try:
        return MigrateSettings()
    except ValidationError:
        raise SettingsError({"APPCONN_DATABASE_URL": "required"}) from None


def _cmd_migrate(settings: MigrateSettings) -> int:
    applied = run_migrations(settings.database_url.get_secret_value(), "agents_app_connector")
    log.info("migrations applied: %s", ", ".join(applied) or "none")
    return 0


def _cmd_admin(settings: Settings) -> int:
    async def main(stop: asyncio.Event) -> None:
        pool = await open_pool(settings.database_url.get_secret_value())
        try:
            app = create_admin_app(pool, settings)
            await serve_asgi(
                app, settings.http_host, settings.http_port, settings.shutdown_grace_seconds, stop
            )
        finally:
            await pool.close()

    return asyncio.run(run_service(main, grace_seconds=settings.shutdown_grace_seconds))


def _cmd_app(settings: Settings) -> int:
    async def main(stop: asyncio.Event) -> None:
        await run_app(settings, stop=stop)

    return asyncio.run(run_service(main, grace_seconds=settings.shutdown_grace_seconds))


def _cmd_purge(settings: Settings) -> int:
    async def main() -> PurgeResult:
        pool = await open_pool(settings.database_url.get_secret_value())
        try:
            return await purge(
                pool,
                AttachmentStore(Path(settings.data_dir)),
                utcnow(),
                retention_days=settings.retention_days,
            )
        finally:
            await pool.close()

    result = asyncio.run(main())
    print(f"purged files={result.files} bytes={result.bytes}")
    return 0


COMMANDS: dict[str, tuple[Callable, Callable]] = {
    "migrate": (_load_migrate, _cmd_migrate),
    "admin": (lambda: load_settings(Settings), _cmd_admin),
    "app": (lambda: load_settings(Settings), _cmd_app),
    "purge": (lambda: load_settings(Settings), _cmd_purge),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agents-app-connector", description="Agents app connector service"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{" + ",".join(SUBCOMMANDS) + "}")
    for name, help_text in SUBCOMMANDS.items():
        sub.add_parser(name, help=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    load, run = COMMANDS[args.command]
    try:
        settings = load()
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        return EXIT_BAD_SETTINGS
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    # httpx logs every request URL, query included, at INFO; the task relay logs only paths
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
