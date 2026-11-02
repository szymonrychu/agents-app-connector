# agents-app-connector

Part of the claude-code-tatara project (python).

## What this is

A Python microservice with its own Postgres and a PVC: a client-plugin connector for user
devices. The `sketch` plugin pairs Boox devices, takes sketch uploads, translates them into
versioned markdown specs through a sketch-translator agent session, streams progress back and
hands specs off to other sessions.

## Development

Run `pre-commit install` once per clone to enable the gitleaks hook.

- `mise run test` runs the test suite.
- `mise run lint` runs ruff check and format check.
- DB tests need Docker (testcontainers).

## Configuration

Environment variables use the `APPCONN_` prefix. Required: `APPCONN_DATABASE_URL`,
`APPCONN_ORCHESTRATOR_URL` and `APPCONN_ORCHESTRATOR_TOKEN` (or `..._TOKEN_FILE`). The
remaining settings and their defaults are in `src/agents_app_connector/settings.py`.

## Subcommands

`agents-app-connector {migrate,admin,app,purge}`; a bad configuration exits with code 78.
