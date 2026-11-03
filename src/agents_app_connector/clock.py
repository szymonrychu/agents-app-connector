"""The one source of 'now', so tests can patch it."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    return datetime.now(UTC)
