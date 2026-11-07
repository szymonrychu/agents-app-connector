"""Connector-assigned line ids of a spec version."""

import pytest

from agents_app_connector.line_ids import LineIdError, LineProvenance, assign
from agents_app_connector.plugins.sketch.layout import SpecLayout, SpecLine

BASE = SpecLayout(
    lines=[
        SpecLine(line_id="L3", source_line=1),
        SpecLine(line_id="L7", source_line=2),
    ]
)


def ids(lines):
    return [(x.line_id, x.source_line, x.origin) for x in lines]


def test_new_lines_numbered_after_max():
    out = assign(
        "a\nb\nc\n",
        [LineProvenance(n=1, segments=["p0-r1-0"]), LineProvenance(n=3, op="op-1")],
        None,
        next_number=8,
    )
    assert ids(out) == [
        ("L8", 1, {"segments": ["p0-r1-0"]}),
        ("L9", 2, None),
        ("L10", 3, {"op": "op-1"}),
    ]


def test_kept_ids_survive():
    out = assign(
        "new\nkept a\nkept b changed",
        [
            LineProvenance(n=2, kept="L3"),
            LineProvenance(n=3, kept="L7", op="op-2"),
        ],
        BASE,
        next_number=8,
    )
    assert ids(out) == [
        ("L8", 1, None),
        ("L3", 2, {"kept": "L3"}),
        ("L7", 3, {"kept": "L7", "op": "op-2"}),
    ]


def test_unknown_kept_id_raises():
    with pytest.raises(LineIdError, match="L5"):
        assign("a", [LineProvenance(n=1, kept="L5")], BASE, next_number=8)
    with pytest.raises(LineIdError, match="L3"):
        assign("a", [LineProvenance(n=1, kept="L3")], None, next_number=1)


def test_kept_twice_raises():
    with pytest.raises(LineIdError, match="L3"):
        assign(
            "a\nb",
            [LineProvenance(n=1, kept="L3"), LineProvenance(n=2, kept="L3")],
            BASE,
            next_number=8,
        )


def test_blank_lines_get_ids():
    out = assign("a\n\n```\nx\n```\n", None, None, next_number=1)
    assert [x.line_id for x in out] == ["L1", "L2", "L3", "L4", "L5"]
    assert [x.source_line for x in out] == [1, 2, 3, 4, 5]


def test_line_number_out_of_range_or_twice_raises():
    with pytest.raises(LineIdError, match="3"):
        assign("a\nb", [LineProvenance(n=3)], None, next_number=1)
    with pytest.raises(LineIdError, match="1"):
        assign("a\nb", [LineProvenance(n=1), LineProvenance(n=1)], None, next_number=1)
