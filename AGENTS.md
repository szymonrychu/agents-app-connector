# agents-app-connector

Component `agents-app-connector` (python) of the claude-code-tatara project. Its own tests, `MEMORY.md` and `ROADMAP.md` live here.

Run `mise run test`, `mise run lint` and `mise run fmt` from this directory.

## Commands

- `mise run test` runs the suite; DB tests need Docker (testcontainers).
- `mise run lint` runs ruff check and format check; `mise run fmt` formats.
- `mise run test:chart` and `mise run lint:chart` check the helm chart.

## Highest-risk surface

Public device API: a device token is a bearer secret; untrusted images become agent input;
tokens are stored only as sha256. Do not weaken these.

## Public repo rule

Treat this repo as public: no secrets, credentials, internal hostnames, cluster names or
personal data in files or history. Deployment-specific values live only in agents-helmfile.
