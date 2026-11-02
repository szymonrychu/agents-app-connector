"""Create or update the sketch-translator preset through the orchestrator admin API.

    python scripts/ensure_preset.py --base-url $ORCH_AGENT_URL/agent/v1/admin

The bearer token is read from the environment variable ORCH_AGENT_TOKEN (never from argv) and
is never printed. A failure prints the HTTP status and the error code only.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Literal

import httpx

from agents_app_connector.plugins.sketch import load_preset

TOKEN_ENV = "ORCH_AGENT_TOKEN"


class EnsureError(Exception):
    pass


def _check(response: httpx.Response) -> httpx.Response:
    if response.is_success:
        return response
    try:
        code = response.json().get("error", "unknown")
    except (ValueError, AttributeError):
        code = "unknown"
    raise EnsureError(f"HTTP {response.status_code} {code}")


def ensure_preset(
    client: httpx.Client, base: str, preset: dict
) -> Literal["created", "updated", "unchanged"]:
    """GET by name; 404 creates, a differing spec or flag updates, equal leaves it.

    The admin routes show a superleader only presets with superleader_manageable, so the flag
    is always sent as true: without it the next GET or PUT would answer 404.
    """
    base = base.rstrip("/")
    body = {**preset, "superleader_manageable": True}
    url = f"{base}/presets/{preset['name']}"
    found = client.get(url)
    if found.status_code == 404:
        _check(client.post(f"{base}/presets", json=body))
        return "created"
    current = _check(found).json()
    if current.get("spec") == body["spec"] and current.get("superleader_manageable") is True:
        return "unchanged"
    _check(client.put(url, json=body))
    return "updated"


def main(argv: list[str] | None = None, transport: httpx.BaseTransport | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True, help="orchestrator admin API base URL")
    args = parser.parse_args(argv)
    token = os.environ.get(TOKEN_ENV)
    if not token:
        print(f"{TOKEN_ENV} is not set", file=sys.stderr)
        return 1
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(headers=headers, transport=transport, timeout=30) as client:
            print(ensure_preset(client, args.base_url, load_preset()))
    except EnsureError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
