"""README API coverage: every route of both apps is documented, none invented."""

import re
from pathlib import Path

from agents_app_connector.api.openapi import route_keys
from agents_app_connector.settings import Settings

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text(encoding="utf-8")


def section(title: str) -> str:
    """The body of the `## title` section (up to the next H2)."""
    match = re.search(rf"^## {re.escape(title)}\s*$", README, re.MULTILINE)
    assert match, f"README has no '## {title}' section"
    rest = README[match.end() :]
    nxt = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: nxt.start()] if nxt else rest


def readme_routes() -> set[str]:
    found = re.findall(
        r"^### `(GET|POST|PATCH|PUT|DELETE) (/app/\S*)`\s*$",
        section("API"),
        re.MULTILINE,
    )
    return {f"{m} {p}" for m, p in found}


def test_readme_lists_every_route():
    expected = {k for k in route_keys() if " /app/" in k}
    assert len(expected) == 17
    assert expected - readme_routes() == set(), "undocumented routes"
    assert readme_routes() - expected == set(), "README documents routes the app lacks"


def test_readme_documents_subcommands_and_settings():
    commands = section("Subcommands")
    for name in ("migrate", "admin", "app", "purge"):
        assert f"`agents-app-connector {name}`" in commands
    config = section("Configuration")
    for name in Settings.model_fields:
        assert f"`APPCONN_{name.upper()}`" in config, name


def test_readme_is_ascii():
    assert README.isascii()
