"""Stable line ids of spec versions written from notebook pages.

The connector assigns them at `document_write`: a line the translator reports as `kept` keeps
that id of the base version (each kept id once); every other source line gets the next `L<n>`
of the conversation, in source order. Ids are monotonic per conversation and never reused, so
the caller passes `next_number` = 1 + the largest number in any stored version (also after a
revert).
"""

from pydantic import BaseModel, ConfigDict, Field

from agents_app_connector.plugins.sketch.layout import SpecLayout, SpecLine


class LineIdError(ValueError):
    """The reported provenance does not fit the document or the base version."""


class LineProvenance(BaseModel):
    """Where source line `n` (1-based) of the written document came from."""

    model_config = ConfigDict(extra="forbid")

    n: int = Field(ge=1)
    segments: list[str] = []
    kept: str | None = None
    op: str | None = None


def _origin(item: LineProvenance | None) -> dict | None:
    if item is None:
        return None
    origin: dict = {}
    if item.segments:
        origin["segments"] = list(item.segments)
    if item.kept is not None:
        origin["kept"] = item.kept
    if item.op is not None:
        origin["op"] = item.op
    return origin or None


def assign(
    markdown: str,
    lines: list[LineProvenance] | None,
    base: SpecLayout | None,
    next_number: int,
) -> list[SpecLine]:
    """One SpecLine per markdown source line, with its id and origin."""
    count = len(markdown.splitlines())
    by_n: dict[int, LineProvenance] = {}
    for item in lines or []:
        if item.n > count:
            raise LineIdError(f"line {item.n} is past the last source line {count}")
        if item.n in by_n:
            raise LineIdError(f"line {item.n} is reported twice")
        by_n[item.n] = item
    known = set() if base is None else {line.line_id for line in base.lines}
    kept: set[str] = set()
    out: list[SpecLine] = []
    number = next_number
    for n in range(1, count + 1):
        item = by_n.get(n)
        if item is not None and item.kept is not None:
            if item.kept not in known:
                raise LineIdError(f"kept line id {item.kept} is not in the base version")
            if item.kept in kept:
                raise LineIdError(f"kept line id {item.kept} is used twice")
            kept.add(item.kept)
            line_id = item.kept
        else:
            line_id = f"L{number}"
            number += 1
        out.append(SpecLine(line_id=line_id, source_line=n, origin=_origin(item)))
    return out
